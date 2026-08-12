"""Deterministic population-proportional N100 semantic-coverage planning.

This module is deliberately separate from the frozen N10 planner.  It reads
immutable E2/E3 artifacts plus the completed N10 manifest/sidecar, removes the
prior buildings and image identities, apportions 100 seats with hierarchical
Hamilton quotas, and freezes the same representative-plus-coverage records
used by the semantic Vision runtime.  It performs no network or model work.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    MAX_OCCURRENCES_PER_BUILDING,
    P2_POLICY_ID,
    BuildingInventoryItem,
    select_building_coverage,
)
from canonical.cross_source_semantic_coverage_sources import (
    ArtifactSnapshot,
    ArtifactSpec,
    collect_inventory,
    inspect_artifact,
    load_building_candidates,
    sha256_file,
    sqlite_sidecars,
)


SEMANTIC_COVERAGE_N100_VERSION = "archibe-cross-source-semantic-coverage-n100-v1"
SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN = (
    "archibe-cross-source-semantic-coverage-n100-manifest-v1"
)
N100_SCORE_DOMAIN = "semantic-coverage-n100-stratum-score-v1"
N100_QUOTA_VERSION = "source-then-stratum-hamilton-v1"
DEFAULT_N100_SAMPLE_SEED = "archibe-semantic-coverage-n100-v1"
DEFAULT_N100_SAMPLE_SIZE = 100
EXPECTED_PRODUCTION_OCCURRENCES = 562
EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS = 562

DEFAULT_N10_MANIFEST_RELATIVE_PATH = Path(
    "data/reports/cross_source_semantic_coverage_n10_v1.json"
)
DEFAULT_N10_SIDECAR_RELATIVE_PATH = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db"
)
DEFAULT_N10_MANIFEST_SIZE = 147_921
DEFAULT_N10_MANIFEST_BYTE_SHA256 = (
    "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f"
)
DEFAULT_N10_MANIFEST_SELF_SHA256 = (
    "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b"
)
DEFAULT_N10_SIDECAR_SIZE = 835_584
DEFAULT_N10_SIDECAR_BYTE_SHA256 = (
    "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a"
)
DEFAULT_N10_SIDECAR_LOGICAL_SHA256 = (
    "10de6fc2a4678c0566beebd93774e97776c2e28239a48f01f4a0ef02001e65dc"
)
DEFAULT_N10_RUN_ID = "semn10-bf2318bd8942ab13ea28c121"
DEFAULT_N10_APPLICATION_ID = 0x53564E31
DEFAULT_N10_USER_VERSION = 1
N10_MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-manifest-v1"


def _identity(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


@dataclass(frozen=True)
class PriorN10Spec:
    manifest_path: Path
    sidecar_path: Path
    expected_manifest_size: int = DEFAULT_N10_MANIFEST_SIZE
    expected_manifest_byte_sha256: str = DEFAULT_N10_MANIFEST_BYTE_SHA256
    expected_manifest_self_sha256: str = DEFAULT_N10_MANIFEST_SELF_SHA256
    expected_sidecar_size: int = DEFAULT_N10_SIDECAR_SIZE
    expected_sidecar_byte_sha256: str = DEFAULT_N10_SIDECAR_BYTE_SHA256
    expected_sidecar_logical_sha256: str = DEFAULT_N10_SIDECAR_LOGICAL_SHA256
    expected_run_id: str = DEFAULT_N10_RUN_ID
    expected_application_id: int = DEFAULT_N10_APPLICATION_ID
    expected_user_version: int = DEFAULT_N10_USER_VERSION


@dataclass(frozen=True)
class N10Exclusions:
    manifest_input: dict[str, Any]
    sidecar_input: dict[str, Any]
    building_records: tuple[dict[str, Any], ...]
    occurrence_records: tuple[dict[str, Any], ...]

    @property
    def building_ids(self) -> frozenset[str]:
        return frozenset(str(value["selection_id"]) for value in self.building_records)

    @property
    def e1_pixels(self) -> frozenset[str]:
        values: set[str] = set()
        for record in self.occurrence_records:
            values.add(str(record["expected_e1_pixel_sha256"]))
            values.add(str(record["actual_e1_pixel_sha256"]))
        return frozenset(values)

    @property
    def derivative_pixels(self) -> frozenset[str]:
        return frozenset(
            str(value["derivative_pixel_sha256"]) for value in self.occurrence_records
        )

    @property
    def urls(self) -> frozenset[str]:
        values: set[str] = set()
        for record in self.occurrence_records:
            for key in (
                "canonical_url",
                "fetch_url",
                "planned_final_url",
                "actual_final_url",
            ):
                value = record.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
        return frozenset(values)


@dataclass(frozen=True)
class N100Selection:
    rank: int
    rank_in_stratum: int
    source_quota: int
    stratum_quota: int
    eligible_count: int
    available_count: int
    score_sha256: str
    building: BuildingInventoryItem

    @property
    def guard_name(self) -> str:
        return (
            "n100_hamilton_"
            + self.building.source
            + "_"
            + self.building.population_stratum
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "available_count_before_pick": self.available_count - self.rank_in_stratum + 1,
            "building": self.building.as_record(),
            "building_record_sha256": self.building.record_sha256,
            "eligible_count": self.eligible_count,
            "guard_name": self.guard_name,
            "guard_source": self.building.source,
            "quota_version": N100_QUOTA_VERSION,
            "rank": self.rank,
            "rank_in_stratum": self.rank_in_stratum,
            "score_sha256": self.score_sha256,
            "source_quota": self.source_quota,
            "stratum_key": (
                self.building.source + ":" + self.building.population_stratum
            ),
            "stratum_quota": self.stratum_quota,
        }


def deterministic_n100_score(
    seed: str, source: str, stratum: str, selection_id: str
) -> str:
    return canonical_sha256(
        {
            "domain": N100_SCORE_DOMAIN,
            "population_stratum": _identity(stratum, label="stratum"),
            "seed": _identity(seed, label="seed"),
            "selection_id": _identity(selection_id, label="selection_id"),
            "source": _identity(source, label="source").casefold(),
        }
    )


def _hamilton(
    counts: Mapping[str, int], seats: int
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if seats < 0:
        raise ValueError("Hamilton seats must be non-negative")
    ordered = tuple(sorted(counts))
    if any(not isinstance(counts[key], int) or counts[key] < 0 for key in ordered):
        raise ValueError("Hamilton counts must be non-negative integers")
    total = sum(counts.values())
    if total == 0:
        if seats:
            raise ValueError("cannot apportion positive seats over an empty population")
        return {key: 0 for key in ordered}, []
    if seats > total:
        raise ValueError("cannot sample more rows than the available population")
    floors = {key: counts[key] * seats // total for key in ordered}
    remainders = {key: counts[key] * seats % total for key in ordered}
    remaining = seats - sum(floors.values())
    winners = set(
        sorted(ordered, key=lambda key: (-remainders[key], key))[:remaining]
    )
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
    if sum(quotas.values()) != seats:
        raise AssertionError("Hamilton quota sum mismatch")
    return quotas, records


def hierarchical_hamilton_quotas(
    inventory: Iterable[BuildingInventoryItem],
    *,
    sample_size: int = DEFAULT_N100_SAMPLE_SIZE,
) -> tuple[dict[tuple[str, str], int], dict[str, Any]]:
    """Allocate source seats, then source-local population-stratum seats."""

    values = tuple(inventory)
    if len({value.selection_id for value in values}) != len(values):
        raise ValueError("sampling-frame selection_id values must be unique")
    source_counts = Counter(value.source for value in values)
    source_quotas, source_records = _hamilton(source_counts, sample_size)
    result: dict[tuple[str, str], int] = {}
    stratum_records: list[dict[str, Any]] = []
    for source in sorted(source_counts):
        counts = Counter(
            value.population_stratum for value in values if value.source == source
        )
        quotas, records = _hamilton(counts, source_quotas[source])
        for record in records:
            stratum = str(record.pop("key"))
            result[(source, stratum)] = quotas[stratum]
            stratum_records.append({"source": source, "stratum": stratum, **record})
    if sum(result.values()) != sample_size:
        raise AssertionError("hierarchical Hamilton quota sum mismatch")
    plan = {
        "quota_version": N100_QUOTA_VERSION,
        "sample_size": sample_size,
        "source_level": [
            {"source": str(record.pop("key")), **record} for record in source_records
        ],
        "stratum_level": stratum_records,
    }
    return result, plan


def select_stratified_n100(
    inventory: Iterable[BuildingInventoryItem],
    *,
    excluded_selection_ids: Iterable[str],
    seed: str = DEFAULT_N100_SAMPLE_SEED,
    sample_size: int = DEFAULT_N100_SAMPLE_SIZE,
) -> tuple[tuple[N100Selection, ...], dict[str, Any]]:
    """Select a deterministic population-proportional sample from a clean frame."""

    seed = _identity(seed, label="seed")
    all_values = tuple(sorted(inventory, key=lambda value: value.selection_id))
    excluded = frozenset(excluded_selection_ids)
    frame = tuple(value for value in all_values if value.selection_id not in excluded)
    quotas, quota_plan = hierarchical_hamilton_quotas(frame, sample_size=sample_size)
    eligible_counts = Counter(
        (value.source, value.population_stratum) for value in all_values
    )
    available_counts = Counter((value.source, value.population_stratum) for value in frame)
    chosen: list[N100Selection] = []
    for key in sorted(quotas):
        quota = quotas[key]
        source, stratum = key
        candidates = [
            value
            for value in frame
            if value.source == source and value.population_stratum == stratum
        ]
        scored = sorted(
            (
                deterministic_n100_score(seed, source, stratum, value.selection_id),
                value,
            )
            for value in candidates
        )
        if len(scored) < quota:
            raise ValueError(f"insufficient N100 candidates for {key!r}")
        for rank_in_stratum, (score, building) in enumerate(scored[:quota], 1):
            chosen.append(
                N100Selection(
                    rank=len(chosen) + 1,
                    rank_in_stratum=rank_in_stratum,
                    source_quota=sum(
                        value
                        for (quota_source, _), value in quotas.items()
                        if quota_source == source
                    ),
                    stratum_quota=quota,
                    eligible_count=eligible_counts[key],
                    available_count=available_counts[key],
                    score_sha256=score,
                    building=building,
                )
            )
    if len(chosen) != sample_size:
        raise AssertionError("N100 selection count mismatch")
    return tuple(chosen), quota_plan


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _load_canonical_manifest(path: Path, spec: PriorN10Spec) -> tuple[dict[str, Any], bytes]:
    if sqlite_sidecars(path):
        raise RuntimeError(f"unexpected sidecars beside N10 manifest path: {path}")
    raw = path.read_bytes()
    if len(raw) != spec.expected_manifest_size:
        raise ValueError("N10 manifest size mismatch")
    byte_sha = hashlib.sha256(raw).hexdigest()
    if byte_sha != spec.expected_manifest_byte_sha256:
        raise ValueError("N10 manifest byte SHA mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError("N10 manifest is not canonical JSON plus one LF")
    body = dict(payload)
    self_sha = body.pop("semantic_coverage_manifest_sha256", None)
    replay = canonical_sha256({"domain": N10_MANIFEST_DOMAIN, "manifest": body})
    if self_sha != spec.expected_manifest_self_sha256 or replay != self_sha:
        raise ValueError("N10 manifest self SHA mismatch")
    if payload.get("sample_size_buildings") != 10:
        raise ValueError("prior manifest is not fixed N10")
    return payload, raw


def load_prior_n10_exclusions(spec: PriorN10Spec) -> N10Exclusions:
    """Validate the completed N10 inputs and return its immutable identities."""

    manifest_path = spec.manifest_path.resolve()
    sidecar_path = spec.sidecar_path.resolve()
    payload, manifest_raw = _load_canonical_manifest(manifest_path, spec)
    if sqlite_sidecars(sidecar_path):
        raise RuntimeError(f"N10 sidecar has SQLite sidecars: {sidecar_path}")
    if sidecar_path.stat().st_size != spec.expected_sidecar_size:
        raise ValueError("N10 sidecar size mismatch")
    sidecar_sha = sha256_file(sidecar_path)
    if sidecar_sha != spec.expected_sidecar_byte_sha256:
        raise ValueError("N10 sidecar byte SHA mismatch")
    connection = _open_immutable(sidecar_path)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != spec.expected_application_id:
            raise ValueError("N10 sidecar application_id mismatch")
        if user_version != spec.expected_user_version:
            raise ValueError("N10 sidecar user_version mismatch")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if quick != "ok" or integrity != "ok" or foreign_keys:
            raise ValueError("N10 sidecar SQLite validation failed")
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("N10 sidecar must contain exactly one run")
        run = runs[0]
        if str(run["run_id"]) != spec.expected_run_id or str(run["status"]) != "complete":
            raise ValueError("N10 sidecar run identity/status mismatch")
        if str(run["logical_sha256"]) != spec.expected_sidecar_logical_sha256:
            raise ValueError("N10 sidecar logical SHA mismatch")
        if (
            int(run["manifest_size"]) != len(manifest_raw)
            or str(run["manifest_byte_sha256"]) != spec.expected_manifest_byte_sha256
            or str(run["manifest_self_sha256"]) != spec.expected_manifest_self_sha256
        ):
            raise ValueError("N10 sidecar does not bind the supplied manifest")
        buildings: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM selected_buildings ORDER BY building_rank"
        ):
            record = {
                "building_rank": int(row["building_rank"]),
                "reason": "prior_n10_building",
                "selection_id": str(row["selection_id"]),
                "source": str(row["source"]),
                "source_building_id": str(row["source_building_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            buildings.append(record)
        occurrences: list[dict[str, Any]] = []
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
            wrapper = json.loads(str(row["manifest_json"]))
            candidate = wrapper["occurrence"]["candidate"]
            record = {
                "actual_e1_pixel_sha256": str(row["actual_e1_pixel_sha256"]),
                "actual_final_url": (
                    None if row["actual_final_url"] is None else str(row["actual_final_url"])
                ),
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
            if any(
                len(str(record[key])) != 64
                for key in (
                    "actual_e1_pixel_sha256",
                    "derivative_pixel_sha256",
                    "expected_e1_pixel_sha256",
                )
            ):
                raise ValueError("N10 sidecar contains incomplete image identity")
            record["record_sha256"] = canonical_sha256(record)
            occurrences.append(record)
        if len(buildings) != 10 or len(occurrences) != 57:
            raise ValueError("N10 exclusion accounting mismatch")
        if len({value["selection_id"] for value in buildings}) != 10:
            raise ValueError("N10 building identities are not unique")
        for key in (
            "expected_e1_pixel_sha256",
            "actual_e1_pixel_sha256",
            "derivative_pixel_sha256",
            "fetch_url",
        ):
            if len({value[key] for value in occurrences}) != 57:
                raise ValueError(f"N10 exclusion identity is not unique: {key}")
        manifest_input = {
            "byte_sha256": spec.expected_manifest_byte_sha256,
            "manifest_self_sha256": spec.expected_manifest_self_sha256,
            "ordered_building_manifest_sha256": payload[
                "ordered_building_manifest_sha256"
            ],
            "ordered_occurrence_manifest_sha256": payload[
                "ordered_occurrence_manifest_sha256"
            ],
            "path": str(manifest_path),
            "sample_seed": payload["sample_seed"],
            "size_bytes": len(manifest_raw),
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
    return N10Exclusions(
        manifest_input,
        sidecar_input,
        tuple(buildings),
        tuple(occurrences),
    )


def _collision_buildings(
    e3: sqlite3.Connection,
    *,
    run_id: str,
    prior: N10Exclusions,
) -> tuple[dict[str, Any], ...]:
    pixels = tuple(sorted(prior.e1_pixels))
    urls = tuple(sorted(prior.urls))
    pixel_marks = ",".join("?" for _ in pixels)
    url_marks = ",".join("?" for _ in urls)
    sql = f"""
        SELECT selection_id,source,
               sum(normalized_pixel_sha256 IN ({pixel_marks})) AS pixel_matches,
               sum(canonical_url IN ({url_marks}) OR fetch_url IN ({url_marks})
                   OR final_url IN ({url_marks})) AS url_matches
        FROM image_candidates
        WHERE run_id=? AND (
              normalized_pixel_sha256 IN ({pixel_marks})
              OR canonical_url IN ({url_marks}) OR fetch_url IN ({url_marks})
              OR final_url IN ({url_marks}))
        GROUP BY selection_id,source ORDER BY source,selection_id
    """
    params: list[Any] = [
        *pixels,
        *urls,
        *urls,
        *urls,
        run_id,
        *pixels,
        *urls,
        *urls,
        *urls,
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


def _snapshot_still_matches(spec: ArtifactSpec, snapshot: ArtifactSnapshot) -> None:
    if sqlite_sidecars(spec.path):
        raise RuntimeError(f"SQLite input gained sidecars: {spec.path}")
    if spec.path.stat().st_size != snapshot.size_bytes or sha256_file(spec.path) != snapshot.byte_sha256:
        raise RuntimeError(f"immutable input changed during N100 planning: {spec.name}")


def build_semantic_coverage_n100_manifest(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    n10_spec: PriorN10Spec,
    *,
    seed: str = DEFAULT_N100_SAMPLE_SEED,
    enforce_production_counts: bool = True,
    enforce_production_result: bool = True,
) -> dict[str, Any]:
    """Build the canonical fixed N100 manifest entirely offline."""

    prior = load_prior_n10_exclusions(n10_spec)
    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3_snapshot: ArtifactSnapshot | None = None
    e3: sqlite3.Connection | None = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        inventory, population = collect_inventory(
            e3,
            run_id=e3_snapshot.run_id,
            enforce_production_counts=enforce_production_counts,
        )
        collisions = _collision_buildings(e3, run_id=e3_snapshot.run_id, prior=prior)
        collision_ids = {str(value["selection_id"]) for value in collisions}
        excluded_ids = prior.building_ids | collision_ids
        selected, quota_plan = select_stratified_n100(
            inventory,
            excluded_selection_ids=excluded_ids,
            seed=seed,
        )
        selected_records: list[dict[str, Any]] = []
        occurrence_hashes: list[dict[str, Any]] = []
        url_hashes: list[dict[str, Any]] = []
        pixel_hashes: list[dict[str, Any]] = []
        selected_pixels: list[str] = []
        selected_urls: list[str] = []
        input_rank = 0
        for selection in selected:
            candidates = load_building_candidates(
                e3,
                e2,
                e3_run_id=e3_snapshot.run_id,
                e2_run_id=e2_snapshot.run_id,
                building=selection.building,
            )
            plan = select_building_coverage(candidates)
            selected_record = {
                **selection.as_record(),
                "coverage_plan": plan.as_record(),
                "coverage_plan_record_sha256": plan.record_sha256,
            }
            selected_sha = canonical_sha256(selected_record)
            selected_records.append(
                {
                    "selected_building": selected_record,
                    "selected_building_record_sha256": selected_sha,
                }
            )
            for occurrence in plan.selected_occurrences:
                input_rank += 1
                candidate = occurrence.candidate
                selected_pixels.append(candidate.normalized_pixel_sha256)
                selected_urls.append(candidate.fetch_url)
                occurrence_hashes.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "occurrence_record_sha256": occurrence.record_sha256,
                        "selection_id": selection.building.selection_id,
                    }
                )
                url_hashes.append(
                    {
                        "fetch_url": candidate.fetch_url,
                        "input_rank": input_rank,
                        "source": candidate.source,
                    }
                )
                pixel_hashes.append(
                    {
                        "input_rank": input_rank,
                        "normalized_pixel_sha256": candidate.normalized_pixel_sha256,
                        "source": candidate.source,
                    }
                )
        overlap = {
            "building_count": len({x.building.selection_id for x in selected} & prior.building_ids),
            "e1_pixel_count": len(set(selected_pixels) & prior.e1_pixels),
            "fetch_url_count": len(set(selected_urls) & prior.urls),
        }
        if any(overlap.values()):
            raise ValueError(f"N100 overlaps prior N10 identities: {overlap!r}")
        expected = {
            "buildings": DEFAULT_N100_SAMPLE_SIZE,
            "occurrences": EXPECTED_PRODUCTION_OCCURRENCES,
            "unique_e1_pixels": EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS,
            "unique_fetch_urls": EXPECTED_PRODUCTION_OCCURRENCES,
        }
        actual = {
            "buildings": len(selected),
            "occurrences": len(selected_pixels),
            "unique_e1_pixels": len(set(selected_pixels)),
            "unique_fetch_urls": len(set(selected_urls)),
        }
        if enforce_production_result and actual != expected:
            raise ValueError(f"N100 production result mismatch: {actual!r} != {expected!r}")
        original_counts = Counter((x.source, x.population_stratum) for x in inventory)
        frame = tuple(x for x in inventory if x.selection_id not in excluded_ids)
        frame_counts = Counter((x.source, x.population_stratum) for x in frame)
        selected_counts = Counter(
            (x.building.source, x.building.population_stratum) for x in selected
        )
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
        raise AssertionError("E3 snapshot was not created")
    _snapshot_still_matches(e2_spec, e2_snapshot)
    _snapshot_still_matches(e3_spec, e3_snapshot)
    if sha256_file(n10_spec.manifest_path) != prior.manifest_input["byte_sha256"]:
        raise RuntimeError("N10 manifest changed during N100 planning")
    if sha256_file(n10_spec.sidecar_path) != prior.sidecar_input["byte_sha256"]:
        raise RuntimeError("N10 sidecar changed during N100 planning")
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


def write_semantic_coverage_n100_manifest(
    path: Path | str, manifest: Mapping[str, Any]
) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(manifest))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return output


__all__ = [
    "DEFAULT_N100_SAMPLE_SEED",
    "DEFAULT_N100_SAMPLE_SIZE",
    "DEFAULT_N10_MANIFEST_RELATIVE_PATH",
    "DEFAULT_N10_SIDECAR_RELATIVE_PATH",
    "EXPECTED_PRODUCTION_OCCURRENCES",
    "EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS",
    "N100_QUOTA_VERSION",
    "N100_SCORE_DOMAIN",
    "N100Selection",
    "N10Exclusions",
    "PriorN10Spec",
    "SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN",
    "SEMANTIC_COVERAGE_N100_VERSION",
    "build_semantic_coverage_n100_manifest",
    "deterministic_n100_score",
    "hierarchical_hamilton_quotas",
    "load_prior_n10_exclusions",
    "select_stratified_n100",
    "write_semantic_coverage_n100_manifest",
]
