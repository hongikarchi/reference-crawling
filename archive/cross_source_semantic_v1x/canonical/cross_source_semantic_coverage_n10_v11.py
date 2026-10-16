"""Offline planner for the disjoint semantic Vision N10 v1.1 smoke.

The sample is selected from immutable E2/E3 evidence.  It deliberately does
not read any prior semantic result: eight population-core rows measure the
ordinary workload and two bounded E3-risk sentinels exercise pre-existing
selection branches.  Prior N10/N100 building, URL, expected-E1, and successful
runtime derivative identities are exclusions, never labels or replacement
seeds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import select_building_coverage
from canonical.cross_source_semantic_coverage_sources import (
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E3_RELATIVE_PATH,
    ArtifactSnapshot,
    ArtifactSpec,
    collect_inventory,
    default_specs,
    inspect_artifact,
    load_building_candidates,
    sha256_file,
    sqlite_sidecars,
)


VERSION = "archibe-cross-source-semantic-coverage-n10-v1.1"
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.1-manifest-v1"
DEFAULT_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1.1"
SCORE_DOMAIN = "semantic-coverage-n10-v1.1-two-lane-score-v1"
QUOTA_VERSION = "population-core8-plus-risk2-v1"
EXPECTED_BUILDINGS = 10
EXPECTED_OCCURRENCES = 53

DEFAULT_N10_V1_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1.json")
DEFAULT_N10_V1_DB = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1.db")
DEFAULT_N100_V1_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n100_v1.json")
DEFAULT_N100_V1_DB = Path("data/enrichment/divisare_architizer_semantic_vision_n100_v1.db")


@dataclass(frozen=True)
class PriorRunSpec:
    name: str
    manifest_path: Path
    sidecar_path: Path
    manifest_size: int
    manifest_byte_sha256: str
    manifest_self_sha256: str
    manifest_self_domain: str
    manifest_version: str
    sample_seed: str
    building_count: int
    occurrence_count: int
    sidecar_size: int
    sidecar_byte_sha256: str
    sidecar_logical_sha256: str
    run_id: str
    application_id: int
    user_version: int
    terminal_status: str
    advisory_lock_expected: bool = False


def default_prior_specs(repo_root: Path | str) -> tuple[PriorRunSpec, PriorRunSpec]:
    root = Path(repo_root).resolve()
    return (
        PriorRunSpec(
            "semantic_n10_v1",
            root / DEFAULT_N10_V1_MANIFEST,
            root / DEFAULT_N10_V1_DB,
            147_921,
            "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f",
            "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b",
            "archibe-cross-source-semantic-coverage-manifest-v1",
            "archibe-cross-source-semantic-coverage-v1",
            "archibe-semantic-coverage-n10-v1",
            10,
            57,
            835_584,
            "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a",
            "10de6fc2a4678c0566beebd93774e97776c2e28239a48f01f4a0ef02001e65dc",
            "semn10-bf2318bd8942ab13ea28c121",
            0x53564E31,
            1,
            "complete",
        ),
        PriorRunSpec(
            "semantic_n100_v1",
            root / DEFAULT_N100_V1_MANIFEST,
            root / DEFAULT_N100_V1_DB,
            1_507_312,
            "37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540",
            "9d34f68ceba5dc561169ed5c3b6fecfc9284dc36fb6ffd544be9198f9faf80ec",
            "archibe-cross-source-semantic-coverage-n100-manifest-v1",
            "archibe-cross-source-semantic-coverage-n100-v1",
            "archibe-semantic-coverage-n100-v1",
            100,
            562,
            7_106_560,
            "e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e",
            "717abc3df21c9fa5e182a52fe099d8c52af71ba8c6ef5256673c41ac1c75205d",
            "semn100-fc21358c3bf06b9498cc57f6",
            0x53564E31,
            2,
            "complete_with_failures",
            True,
        ),
    )


QUOTAS: tuple[tuple[str, str, str, int], ...] = (
    ("population_core", "architizer", "ordinary", 5),
    ("population_core", "architizer", "cross_source_candidate", 1),
    ("population_core", "divisare", "ordinary", 2),
    ("risk_sentinel", "architizer", "cover_quality_risk", 1),
    ("risk_sentinel", "divisare", "gallery_fallback", 1),
)


@dataclass(frozen=True)
class PriorIdentityLedger:
    run_inputs: tuple[dict[str, Any], ...]
    building_records: tuple[dict[str, Any], ...]
    occurrence_records: tuple[dict[str, Any], ...]

    @property
    def building_ids(self) -> frozenset[str]:
        return frozenset(str(value["selection_id"]) for value in self.building_records)

    @property
    def expected_e1_pixels(self) -> frozenset[str]:
        return frozenset(str(value["expected_e1_pixel_sha256"]) for value in self.occurrence_records)

    @property
    def successful_derivative_pixels(self) -> frozenset[str]:
        return frozenset(
            str(value["derivative_pixel_sha256"])
            for value in self.occurrence_records
            if value["derivative_pixel_sha256"] is not None
        )

    @property
    def urls(self) -> frozenset[str]:
        result: set[str] = set()
        for value in self.occurrence_records:
            for key in ("canonical_url", "fetch_url", "planned_final_url", "actual_final_url"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    result.add(item)
        return frozenset(result)


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _load_prior(spec: PriorRunSpec) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    manifest_path = spec.manifest_path.resolve()
    sidecar_path = spec.sidecar_path.resolve()
    sidecars = sqlite_sidecars(sidecar_path)
    transaction_sidecars = tuple(
        value for value in sidecars
        if value.name.endswith(("-wal", "-shm", "-journal"))
    )
    locks = tuple(value for value in sidecars if value.name.endswith(".lock"))
    if transaction_sidecars:
        raise RuntimeError(f"prior sidecar has hot SQLite sidecars: {sidecar_path}")
    if bool(locks) != spec.advisory_lock_expected or len(locks) > 1:
        raise RuntimeError(f"prior sidecar advisory-lock state mismatch: {sidecar_path}")
    raw = manifest_path.read_bytes()
    if len(raw) != spec.manifest_size or hashlib.sha256(raw).hexdigest() != spec.manifest_byte_sha256:
        raise ValueError(f"{spec.name} manifest byte identity mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError(f"{spec.name} manifest is not canonical JSON")
    body = dict(payload)
    self_sha = body.pop("semantic_coverage_manifest_sha256", None)
    if (
        self_sha != spec.manifest_self_sha256
        or canonical_sha256({"domain": spec.manifest_self_domain, "manifest": body}) != self_sha
        or payload.get("version") != spec.manifest_version
        or payload.get("sample_seed") != spec.sample_seed
        or payload.get("sample_size_buildings") != spec.building_count
        or payload.get("planned_occurrence_count") != spec.occurrence_count
    ):
        raise ValueError(f"{spec.name} manifest semantic identity mismatch")
    if sidecar_path.stat().st_size != spec.sidecar_size or sha256_file(sidecar_path) != spec.sidecar_byte_sha256:
        raise ValueError(f"{spec.name} sidecar byte identity mismatch")
    connection = _open_immutable(sidecar_path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError(f"{spec.name} must contain one run")
        run = runs[0]
        if (
            app_id != spec.application_id
            or user_version != spec.user_version
            or quick != "ok"
            or integrity != "ok"
            or foreign_keys
            or str(run["run_id"]) != spec.run_id
            or str(run["status"]) != spec.terminal_status
            or str(run["logical_sha256"]) != spec.sidecar_logical_sha256
            or int(run["manifest_size"]) != spec.manifest_size
            or str(run["manifest_byte_sha256"]) != spec.manifest_byte_sha256
            or str(run["manifest_self_sha256"]) != spec.manifest_self_sha256
        ):
            raise ValueError(f"{spec.name} sidecar semantic identity mismatch")
        buildings = []
        for row in connection.execute("SELECT * FROM selected_buildings ORDER BY building_rank"):
            record = {
                "building_rank": int(row["building_rank"]),
                "prior_run": spec.name,
                "selection_id": str(row["selection_id"]),
                "source": str(row["source"]),
                "source_building_id": str(row["source_building_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            buildings.append(record)
        occurrences = []
        rows = connection.execute(
            """
            SELECT o.*,i.status AS input_status,i.actual_e1_pixel_sha256,
                   i.derivative_pixel_sha256,f.final_url AS actual_final_url
            FROM selected_occurrences o
            JOIN vision_inputs i USING(run_id,inference_id)
            LEFT JOIN fetch_attempts f
              ON f.run_id=i.run_id AND f.inference_id=i.inference_id
             AND f.attempt_no=i.selected_fetch_attempt_no
            ORDER BY o.input_rank
            """
        )
        for row in rows:
            wrapper = json.loads(str(row["manifest_json"]))
            candidate = wrapper["occurrence"]["candidate"]
            status = str(row["input_status"])
            derivative = row["derivative_pixel_sha256"]
            if status == "success" and derivative is None:
                raise ValueError(f"{spec.name} successful input lacks derivative identity")
            if status != "success" and derivative is not None:
                raise ValueError(f"{spec.name} failed input unexpectedly has derivative identity")
            record = {
                "actual_final_url": None if row["actual_final_url"] is None else str(row["actual_final_url"]),
                "candidate_id": str(row["candidate_id"]),
                "canonical_url": str(candidate["canonical_url"]),
                "derivative_pixel_sha256": None if derivative is None else str(derivative),
                "expected_e1_pixel_sha256": str(row["expected_e1_pixel_sha256"]),
                "fetch_url": str(row["fetch_url"]),
                "inference_id": str(row["inference_id"]),
                "input_rank": int(row["input_rank"]),
                "input_status": status,
                "pixel_exclusion_scope": (
                    "expected_e1_and_successful_derivative" if status == "success" else "expected_e1_only"
                ),
                "planned_final_url": candidate.get("final_url"),
                "prior_run": spec.name,
                "selection_id": str(row["selection_id"]),
                "source": str(row["source"]),
                "source_asset_id": str(row["source_asset_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            occurrences.append(record)
        if len(buildings) != spec.building_count or len(occurrences) != spec.occurrence_count:
            raise ValueError(f"{spec.name} sidecar row accounting mismatch")
        run_input = {
            "advisory_lock": (
                None
                if not locks
                else {
                    "byte_sha256": sha256_file(locks[0]),
                    "path": str(locks[0].resolve()),
                    "size_bytes": locks[0].stat().st_size,
                }
            ),
            "application_id": app_id,
            "manifest_byte_sha256": spec.manifest_byte_sha256,
            "manifest_path": str(manifest_path),
            "manifest_self_sha256": spec.manifest_self_sha256,
            "manifest_size_bytes": spec.manifest_size,
            "name": spec.name,
            "run_id": spec.run_id,
            "sample_seed": spec.sample_seed,
            "sidecar_byte_sha256": spec.sidecar_byte_sha256,
            "sidecar_logical_sha256": spec.sidecar_logical_sha256,
            "sidecar_path": str(sidecar_path),
            "sidecar_size_bytes": spec.sidecar_size,
            "sqlite_foreign_key_violations": len(foreign_keys),
            "sqlite_integrity_check": integrity,
            "sqlite_quick_check": quick,
            "status": spec.terminal_status,
            "user_version": user_version,
        }
    finally:
        connection.close()
    return run_input, tuple(buildings), tuple(occurrences)


def load_prior_identity_ledger(specs: Iterable[PriorRunSpec]) -> PriorIdentityLedger:
    run_inputs: list[dict[str, Any]] = []
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    for spec in specs:
        run_input, run_buildings, run_occurrences = _load_prior(spec)
        run_inputs.append(run_input)
        buildings.extend(run_buildings)
        occurrences.extend(run_occurrences)
    if len({value["selection_id"] for value in buildings}) != len(buildings):
        raise ValueError("prior runs contain overlapping buildings")
    if len({value["expected_e1_pixel_sha256"] for value in occurrences}) != len(occurrences):
        raise ValueError("prior runs contain overlapping expected E1 pixels")
    return PriorIdentityLedger(tuple(run_inputs), tuple(buildings), tuple(occurrences))


def _candidate_collision_records(
    e3: sqlite3.Connection,
    *,
    run_id: str,
    prior: PriorIdentityLedger,
) -> tuple[dict[str, Any], ...]:
    counts: dict[str, dict[str, Any]] = {}
    pixel_values = sorted(prior.expected_e1_pixels | prior.successful_derivative_pixels)
    for start in range(0, len(pixel_values), 400):
        chunk = pixel_values[start : start + 400]
        marks = ",".join("?" for _ in chunk)
        for row in e3.execute(
            f"SELECT selection_id,source,count(*) FROM image_candidates WHERE run_id=? "
            f"AND normalized_pixel_sha256 IN ({marks}) GROUP BY selection_id,source",
            (run_id, *chunk),
        ):
            value = counts.setdefault(str(row[0]), {"pixel_match_count": 0, "url_match_count": 0, "source": str(row[1])})
            value["pixel_match_count"] += int(row[2])
    urls = sorted(prior.urls)
    for start in range(0, len(urls), 200):
        chunk = urls[start : start + 200]
        marks = ",".join("?" for _ in chunk)
        for row in e3.execute(
            f"SELECT selection_id,source,count(*) FROM image_candidates WHERE run_id=? AND "
            f"(canonical_url IN ({marks}) OR fetch_url IN ({marks}) OR final_url IN ({marks})) "
            "GROUP BY selection_id,source",
            (run_id, *chunk, *chunk, *chunk),
        ):
            value = counts.setdefault(str(row[0]), {"pixel_match_count": 0, "url_match_count": 0, "source": str(row[1])})
            value["url_match_count"] += int(row[2])
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


def deterministic_score(seed: str, lane: str, source: str, stratum: str, selection_id: str) -> str:
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
    """Select the frozen two-lane sample without reading semantic results."""

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
        candidates = [
            value
            for value in frame
            if value.source == source
            and value.population_stratum == stratum
            and value.selection_id not in selected_ids
        ]
        scored = sorted(
            (
                deterministic_score(seed, lane, source, stratum, value.selection_id),
                value,
            )
            for value in candidates
        )
        if len(scored) < quota:
            raise ValueError(f"insufficient candidates for {lane}:{source}:{stratum}")
        for rank_in_quota, (score, building) in enumerate(scored[:quota], 1):
            selected_ids.add(building.selection_id)
            result.append(
                (lane, source, stratum, quota, rank_in_quota, score, building)
            )
    if len(result) != EXPECTED_BUILDINGS:
        raise AssertionError("fixed N10 v1.1 building count mismatch")
    return tuple(result)


def _snapshot_still_matches(spec: ArtifactSpec, before: ArtifactSnapshot) -> None:
    if sqlite_sidecars(spec.path) or spec.path.stat().st_size != before.size_bytes:
        raise RuntimeError(f"input state changed: {spec.name}")
    if sha256_file(spec.path) != before.byte_sha256:
        raise RuntimeError(f"input SHA changed: {spec.name}")


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
        collisions = _candidate_collision_records(e3, run_id=e3_spec.expected_run_id, prior=prior)
        excluded_ids = prior.building_ids | frozenset(value["selection_id"] for value in collisions)
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
                        {"rank": len(occurrence_hashes) + 1, "occurrence_record_sha256": wrapped["occurrence_record_sha256"]}
                    )
                    candidate_hashes.append(
                        {"rank": len(candidate_hashes) + 1, "candidate_id": candidate["candidate_id"]}
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
                    {"selected_building": selected, "selected_building_record_sha256": canonical_sha256(selected)}
                )
        if len(selected_records) != EXPECTED_BUILDINGS or len(selected_pixels) != EXPECTED_OCCURRENCES:
            raise ValueError("production N10 v1.1 count mismatch")
        selected_url_set = set(selected_urls)
        overlaps = {
            "building": len(selected_ids & prior.building_ids),
            "expected_e1_pixel": len(set(selected_pixels) & prior.expected_e1_pixels),
            "prior_successful_derivative_pixel": len(set(selected_pixels) & prior.successful_derivative_pixels),
            "fetch_url": len(selected_url_set & prior.urls),
        }
        if any(overlaps.values()) or len(set(selected_pixels)) != len(selected_pixels) or len(selected_url_set) != len(selected_urls):
            raise ValueError(f"selected N10 v1.1 identity overlap: {overlaps!r}")
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
                value["derivative_pixel_sha256"] is None for value in prior.occurrence_records
            ),
            "identity_ledger_sha256": canonical_sha256(
                {"domain": VERSION + "-prior-identity-ledger", **exclusion_records}
            ),
            "prior_building_count": len(prior.building_ids),
            "prior_run_inputs": list(prior.run_inputs),
            "prior_successful_derivative_pixel_count": len(prior.successful_derivative_pixels),
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
                "selection_id", "expected_e1_pixel_sha256", "successful_derivative_pixel_sha256",
                "canonical_url", "fetch_url", "planned_final_url", "actual_final_url",
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
                        {"rank": index, "sha256": value} for index, value in enumerate(selected_pixels, 1)
                    ],
                    "version": VERSION,
                }
            ),
            "ordered_fetch_url_manifest_sha256": canonical_sha256(
                {
                    "ordered_fetch_urls": [
                        {"rank": index, "url": value} for index, value in enumerate(selected_urls, 1)
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
            "selection_mode": "offline_disjoint_population_core8_plus_e3_risk2_n10_v1.1",
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
        if sha256_file(spec.manifest_path) != spec.manifest_byte_sha256 or sha256_file(spec.sidecar_path) != spec.sidecar_byte_sha256:
            raise RuntimeError(f"prior run input changed: {spec.name}")
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256,
        "e3_selection": e3_snapshot.byte_sha256,
        **{spec.name + "_manifest": spec.manifest_byte_sha256 for spec in prior_specs},
        **{spec.name + "_sidecar": spec.sidecar_byte_sha256 for spec in prior_specs},
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
    "DEFAULT_SAMPLE_SEED", "EXPECTED_BUILDINGS", "EXPECTED_OCCURRENCES",
    "MANIFEST_DOMAIN", "PriorRunSpec", "QUOTAS", "QUOTA_VERSION", "SCORE_DOMAIN",
    "VERSION", "build_manifest", "default_prior_specs", "deterministic_score",
    "load_prior_identity_ledger", "select_fixed_sample", "write_manifest",
]
