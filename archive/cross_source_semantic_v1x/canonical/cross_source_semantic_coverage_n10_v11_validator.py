"""Independent replay validator for semantic-coverage N10 v1.1.

This module intentionally does not import the N10 v1.1 planner.  It reopens
the frozen artifacts, reconstructs both prior-run exclusion ledgers, repeats
selection with its own score/quota implementation, and compares the complete
canonical manifest bytes.
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
from canonical.cross_source_semantic_coverage_sources import (
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


VERSION = "archibe-cross-source-semantic-coverage-n10-v1.1"
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.1-manifest-v1"
DEFAULT_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1.1"
SCORE_DOMAIN = "semantic-coverage-n10-v1.1-two-lane-score-v1"
QUOTA_VERSION = "population-core8-plus-risk2-v1"
EXPECTED_BUILDINGS = 10
EXPECTED_OCCURRENCES = 53
QUOTAS: tuple[tuple[str, str, str, int], ...] = (
    ("population_core", "architizer", "ordinary", 5),
    ("population_core", "architizer", "cross_source_candidate", 1),
    ("population_core", "divisare", "ordinary", 2),
    ("risk_sentinel", "architizer", "cover_quality_risk", 1),
    ("risk_sentinel", "divisare", "gallery_fallback", 1),
)


@dataclass(frozen=True)
class PriorValidationSpec:
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


def default_prior_specs(repo_root: Path | str) -> tuple[PriorValidationSpec, PriorValidationSpec]:
    root = Path(repo_root).resolve()
    return (
        PriorValidationSpec(
            "semantic_n10_v1",
            root / "data/reports/cross_source_semantic_coverage_n10_v1.json",
            root / "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db",
            147_921,
            "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f",
            "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b",
            "archibe-cross-source-semantic-coverage-manifest-v1",
            "archibe-cross-source-semantic-coverage-v1",
            "archibe-semantic-coverage-n10-v1",
            10, 57, 835_584,
            "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a",
            "10de6fc2a4678c0566beebd93774e97776c2e28239a48f01f4a0ef02001e65dc",
            "semn10-bf2318bd8942ab13ea28c121", 0x53564E31, 1, "complete",
        ),
        PriorValidationSpec(
            "semantic_n100_v1",
            root / "data/reports/cross_source_semantic_coverage_n100_v1.json",
            root / "data/enrichment/divisare_architizer_semantic_vision_n100_v1.db",
            1_507_312,
            "37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540",
            "9d34f68ceba5dc561169ed5c3b6fecfc9284dc36fb6ffd544be9198f9faf80ec",
            "archibe-cross-source-semantic-coverage-n100-manifest-v1",
            "archibe-cross-source-semantic-coverage-n100-v1",
            "archibe-semantic-coverage-n100-v1",
            100, 562, 7_106_560,
            "e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e",
            "717abc3df21c9fa5e182a52fe099d8c52af71ba8c6ef5256673c41ac1c75205d",
            "semn100-fc21358c3bf06b9498cc57f6", 0x53564E31, 2,
            "complete_with_failures",
            True,
        ),
    )


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    passed: bool
    expected: str
    actual: str

    def as_record(self) -> dict[str, Any]:
        return {"actual": self.actual, "expected": self.expected, "name": self.name, "passed": self.passed}


@dataclass(frozen=True)
class ValidationResult:
    manifest_path: Path
    passed: bool
    checks: tuple[ValidationCheck, ...]
    manifest_size: int
    manifest_byte_sha256: str
    manifest_self_sha256: str | None
    selected_buildings: int
    selected_occurrences: int

    def as_record(self) -> dict[str, Any]:
        return {
            "checks": [value.as_record() for value in self.checks],
            "llm_requests": 0,
            "manifest_byte_sha256": self.manifest_byte_sha256,
            "manifest_path": str(self.manifest_path),
            "manifest_self_sha256": self.manifest_self_sha256,
            "manifest_size": self.manifest_size,
            "network_requests": 0,
            "passed": self.passed,
            "selected_buildings": self.selected_buildings,
            "selected_occurrences": self.selected_occurrences,
            "vision_requests": 0,
        }


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _prior_replay(spec: PriorValidationSpec) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    raw = spec.manifest_path.resolve().read_bytes()
    if len(raw) != spec.manifest_size or hashlib.sha256(raw).hexdigest() != spec.manifest_byte_sha256:
        raise ValueError(f"validator {spec.name} manifest bytes mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError(f"validator {spec.name} manifest is noncanonical")
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
        raise ValueError(f"validator {spec.name} manifest fields mismatch")
    path = spec.sidecar_path.resolve()
    sidecars = sqlite_sidecars(path)
    transaction_sidecars = tuple(
        value for value in sidecars
        if value.name.endswith(("-wal", "-shm", "-journal"))
    )
    locks = tuple(value for value in sidecars if value.name.endswith(".lock"))
    if (
        transaction_sidecars
        or bool(locks) != spec.advisory_lock_expected
        or len(locks) > 1
        or path.stat().st_size != spec.sidecar_size
        or sha256_file(path) != spec.sidecar_byte_sha256
    ):
        raise ValueError(f"validator {spec.name} sidecar bytes/state mismatch")
    connection = _open(path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        fk = list(connection.execute("PRAGMA foreign_key_check"))
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("validator prior sidecar run count mismatch")
        run = runs[0]
        if (
            app_id != spec.application_id or user_version != spec.user_version
            or quick != "ok" or integrity != "ok" or fk
            or str(run["run_id"]) != spec.run_id
            or str(run["status"]) != spec.terminal_status
            or str(run["logical_sha256"]) != spec.sidecar_logical_sha256
            or int(run["manifest_size"]) != spec.manifest_size
            or str(run["manifest_byte_sha256"]) != spec.manifest_byte_sha256
            or str(run["manifest_self_sha256"]) != spec.manifest_self_sha256
        ):
            raise ValueError(f"validator {spec.name} sidecar fields mismatch")
        buildings: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM selected_buildings ORDER BY building_rank"):
            record = {
                "building_rank": int(row["building_rank"]), "prior_run": spec.name,
                "selection_id": str(row["selection_id"]), "source": str(row["source"]),
                "source_building_id": str(row["source_building_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            buildings.append(record)
        occurrences: list[dict[str, Any]] = []
        rows = connection.execute(
            """
            SELECT o.*,i.status AS input_status,i.derivative_pixel_sha256,
                   f.final_url AS actual_final_url
            FROM selected_occurrences o JOIN vision_inputs i USING(run_id,inference_id)
            LEFT JOIN fetch_attempts f
              ON f.run_id=i.run_id AND f.inference_id=i.inference_id
             AND f.attempt_no=i.selected_fetch_attempt_no
            ORDER BY o.input_rank
            """
        )
        for row in rows:
            candidate = json.loads(str(row["manifest_json"]))["occurrence"]["candidate"]
            status = str(row["input_status"])
            derivative = row["derivative_pixel_sha256"]
            if (status == "success") != (derivative is not None):
                raise ValueError("validator prior derivative/status mismatch")
            record = {
                "actual_final_url": None if row["actual_final_url"] is None else str(row["actual_final_url"]),
                "candidate_id": str(row["candidate_id"]),
                "canonical_url": str(candidate["canonical_url"]),
                "derivative_pixel_sha256": None if derivative is None else str(derivative),
                "expected_e1_pixel_sha256": str(row["expected_e1_pixel_sha256"]),
                "fetch_url": str(row["fetch_url"]),
                "inference_id": str(row["inference_id"]), "input_rank": int(row["input_rank"]),
                "input_status": status,
                "pixel_exclusion_scope": "expected_e1_and_successful_derivative" if status == "success" else "expected_e1_only",
                "planned_final_url": candidate.get("final_url"), "prior_run": spec.name,
                "selection_id": str(row["selection_id"]), "source": str(row["source"]),
                "source_asset_id": str(row["source_asset_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            occurrences.append(record)
        if len(buildings) != spec.building_count or len(occurrences) != spec.occurrence_count:
            raise ValueError("validator prior row count mismatch")
        run_input = {
            "advisory_lock": (
                None if not locks else {
                    "byte_sha256": sha256_file(locks[0]),
                    "path": str(locks[0].resolve()),
                    "size_bytes": locks[0].stat().st_size,
                }
            ),
            "application_id": app_id, "manifest_byte_sha256": spec.manifest_byte_sha256,
            "manifest_path": str(spec.manifest_path.resolve()),
            "manifest_self_sha256": spec.manifest_self_sha256,
            "manifest_size_bytes": spec.manifest_size, "name": spec.name,
            "run_id": spec.run_id, "sample_seed": spec.sample_seed,
            "sidecar_byte_sha256": spec.sidecar_byte_sha256,
            "sidecar_logical_sha256": spec.sidecar_logical_sha256,
            "sidecar_path": str(path), "sidecar_size_bytes": spec.sidecar_size,
            "sqlite_foreign_key_violations": len(fk), "sqlite_integrity_check": integrity,
            "sqlite_quick_check": quick, "status": spec.terminal_status,
            "user_version": user_version,
        }
    finally:
        connection.close()
    return run_input, buildings, occurrences


def _prior_sets(occurrences: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    expected = {str(value["expected_e1_pixel_sha256"]) for value in occurrences}
    derivatives = {
        str(value["derivative_pixel_sha256"]) for value in occurrences
        if value["derivative_pixel_sha256"] is not None
    }
    urls: set[str] = set()
    for value in occurrences:
        for key in ("canonical_url", "fetch_url", "planned_final_url", "actual_final_url"):
            item = value.get(key)
            if isinstance(item, str) and item:
                urls.add(item)
    return expected, derivatives, urls


def _collisions(
    e3: sqlite3.Connection, run_id: str, pixels: set[str], urls: set[str]
) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    values = sorted(pixels)
    for start in range(0, len(values), 400):
        chunk = values[start : start + 400]
        marks = ",".join("?" for _ in chunk)
        for row in e3.execute(
            f"SELECT selection_id,source,count(*) FROM image_candidates WHERE run_id=? "
            f"AND normalized_pixel_sha256 IN ({marks}) GROUP BY selection_id,source",
            (run_id, *chunk),
        ):
            item = counts.setdefault(str(row[0]), {"pixel_match_count": 0, "url_match_count": 0, "source": str(row[1])})
            item["pixel_match_count"] += int(row[2])
    values = sorted(urls)
    for start in range(0, len(values), 200):
        chunk = values[start : start + 200]
        marks = ",".join("?" for _ in chunk)
        for row in e3.execute(
            f"SELECT selection_id,source,count(*) FROM image_candidates WHERE run_id=? AND "
            f"(canonical_url IN ({marks}) OR fetch_url IN ({marks}) OR final_url IN ({marks})) "
            "GROUP BY selection_id,source",
            (run_id, *chunk, *chunk, *chunk),
        ):
            item = counts.setdefault(str(row[0]), {"pixel_match_count": 0, "url_match_count": 0, "source": str(row[1])})
            item["url_match_count"] += int(row[2])
    result = []
    for selection_id in sorted(counts):
        value = counts[selection_id]
        record = {
            "pixel_match_count": value["pixel_match_count"],
            "reason": "prior_semantic_expected_derivative_or_url_identity",
            "selection_id": selection_id, "source": value["source"],
            "url_match_count": value["url_match_count"],
        }
        record["record_sha256"] = canonical_sha256(record)
        result.append(record)
    return result


def _score(seed: str, lane: str, source: str, stratum: str, selection_id: str) -> str:
    return canonical_sha256({
        "domain": SCORE_DOMAIN, "lane": lane, "population_stratum": stratum,
        "seed": seed, "selection_id": selection_id, "source": source,
    })


def _replay(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    prior_specs: Iterable[PriorValidationSpec],
    seed: str,
) -> dict[str, Any]:
    run_inputs: list[dict[str, Any]] = []
    building_records: list[dict[str, Any]] = []
    occurrence_records: list[dict[str, Any]] = []
    prior_specs = tuple(prior_specs)
    for spec in prior_specs:
        run_input, buildings, occurrences = _prior_replay(spec)
        run_inputs.append(run_input); building_records.extend(buildings); occurrence_records.extend(occurrences)
    prior_buildings = {str(value["selection_id"]) for value in building_records}
    expected_pixels, derivative_pixels, prior_urls = _prior_sets(occurrence_records)
    if len(prior_buildings) != 110 or len(expected_pixels) != 619 or len(derivative_pixels) != 612:
        raise ValueError("validator combined prior identity count mismatch")
    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3 = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        inventory, population = _collect_inventory_independently(
            e2=e2, e3=e3, e2_run_id=e2_spec.expected_run_id,
            e3_run_id=e3_spec.expected_run_id,
        )
        collisions = _collisions(e3, e3_spec.expected_run_id, expected_pixels | derivative_pixels, prior_urls)
        excluded = prior_buildings | {str(value["selection_id"]) for value in collisions}
        frame = tuple(value for value in inventory if value.selection_id not in excluded)
        available = Counter((value.source, value.population_stratum) for value in frame)
        selected_ids: set[str] = set(); selected_records = []
        selected_pixels: list[str] = []; selected_urls: list[str] = []
        occurrence_hashes = []; candidate_hashes = []
        for lane, source, stratum, quota in QUOTAS:
            candidates = [
                value for value in frame if value.source == source
                and value.population_stratum == stratum and value.selection_id not in selected_ids
            ]
            scored = sorted((_score(seed, lane, source, stratum, value.selection_id), value) for value in candidates)
            if len(scored) < quota:
                raise ValueError("validator quota lacks candidates")
            for rank_in_quota, (score, building) in enumerate(scored[:quota], 1):
                selected_ids.add(building.selection_id)
                coverage = _replay_building_coverage(
                    _load_candidates_independently(
                        e2=e2, e3=e3, e2_run_id=e2_spec.expected_run_id,
                        e3_run_id=e3_spec.expected_run_id, building=building,
                    )
                )
                for wrapped in coverage["selected_occurrences"]:
                    candidate = wrapped["occurrence"]["candidate"]
                    selected_pixels.append(str(candidate["normalized_pixel_sha256"])); selected_urls.append(str(candidate["fetch_url"]))
                    occurrence_hashes.append({"rank": len(occurrence_hashes) + 1, "occurrence_record_sha256": wrapped["occurrence_record_sha256"]})
                    candidate_hashes.append({"rank": len(candidate_hashes) + 1, "candidate_id": candidate["candidate_id"]})
                selected = {
                    "available_count": available[(source, stratum)],
                    "building": _inventory_record(building),
                    "building_record_sha256": _inventory_sha256(building),
                    "coverage_plan": coverage,
                    "coverage_record_sha256": canonical_sha256(coverage),
                    "lane": lane, "model_result_selection_used": False, "quota": quota,
                    "rank": len(selected_records) + 1, "rank_in_quota": rank_in_quota,
                    "score_sha256": score, "source": source, "stratum": stratum,
                }
                selected_records.append({"selected_building": selected, "selected_building_record_sha256": canonical_sha256(selected)})
        if len(selected_records) != EXPECTED_BUILDINGS or len(selected_pixels) != EXPECTED_OCCURRENCES:
            raise ValueError("validator production selection count mismatch")
        overlaps = {
            "building": len(selected_ids & prior_buildings),
            "expected_e1_pixel": len(set(selected_pixels) & expected_pixels),
            "prior_successful_derivative_pixel": len(set(selected_pixels) & derivative_pixels),
            "fetch_url": len(set(selected_urls) & prior_urls),
        }
        if any(overlaps.values()) or len(set(selected_pixels)) != 53 or len(set(selected_urls)) != 53:
            raise ValueError("validator selection overlap/duplicate")
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
            "candidate_collision_buildings": len(collisions), "excluded_buildings": len(excluded),
            "expected_e1_pixel_count": len(expected_pixels),
            "failed_expected_only_occurrences": sum(value["derivative_pixel_sha256"] is None for value in occurrence_records),
            "identity_ledger_sha256": canonical_sha256({"domain": VERSION + "-prior-identity-ledger", **records}),
            "prior_building_count": len(prior_buildings), "prior_run_inputs": run_inputs,
            "prior_successful_derivative_pixel_count": len(derivative_pixels),
            "prior_unique_url_count": len(prior_urls), "records": records,
            "selected_plan_overlap": overlaps,
        }
        quota_plan = {
            "quota_version": QUOTA_VERSION,
            "quotas": [{"count": count, "lane": lane, "source": source, "stratum": stratum} for lane, source, stratum, count in QUOTAS],
            "risk_sentinel_basis": (
                "bounded pre-existing E3 population strata only; no prior semantic labels, "
                "known inference IDs, or result errors participate in selection"
            ),
        }
        config = {
            "creates_final_representative": False, "creates_vision_tasks": False,
            "max_occurrences_per_building": 6, "model_result_selection_used": False,
            "prior_exclusion_fields": [
                "selection_id", "expected_e1_pixel_sha256", "successful_derivative_pixel_sha256",
                "canonical_url", "fetch_url", "planned_final_url", "actual_final_url",
            ],
            "quota_version": QUOTA_VERSION, "sample_size": EXPECTED_BUILDINGS,
            "score_domain": SCORE_DOMAIN, "selection_version": VERSION,
        }
        frame_counts = Counter((value.source, value.population_stratum) for value in frame)
        body: dict[str, Any] = {
            "authoritative": False, "config": config, "config_sha256": canonical_sha256(config),
            "creates_final_representative": False, "creates_vision_tasks": False,
            "e2_input": e2_snapshot.as_record(), "e3_input": e3_snapshot.as_record(),
            "exact_reuse_is_provisional_until_vision_input_identity": True,
            "exclusions": exclusions, "llm_requests": 0, "network_requests": 0,
            "ordered_building_manifest_sha256": canonical_sha256({
                "ordered_buildings": [{"rank": index, "record_sha256": value["selected_building_record_sha256"]} for index, value in enumerate(selected_records, 1)], "version": VERSION,
            }),
            "ordered_candidate_id_manifest_sha256": canonical_sha256({"ordered_candidate_ids": candidate_hashes, "version": VERSION}),
            "ordered_e1_pixel_manifest_sha256": canonical_sha256({"ordered_e1_pixels": [{"rank": index, "sha256": value} for index, value in enumerate(selected_pixels, 1)], "version": VERSION}),
            "ordered_fetch_url_manifest_sha256": canonical_sha256({"ordered_fetch_urls": [{"rank": index, "url": value} for index, value in enumerate(selected_urls, 1)], "version": VERSION}),
            "ordered_occurrence_manifest_sha256": canonical_sha256({"ordered_occurrences": occurrence_hashes, "version": VERSION}),
            "planned_e1_exact_duplicate_savings": 0, "planned_occurrence_count": len(selected_pixels),
            "planned_unique_e1_pixel_count": len(set(selected_pixels)), "population": population,
            "quota_plan": quota_plan, "sample_seed": seed, "sample_size_buildings": len(selected_records),
            "sampling_frame": {
                "available_buildings": len(frame),
                "available_by_source_stratum": [{"count": count, "source": source, "stratum": stratum} for (source, stratum), count in sorted(frame_counts.items())],
                "candidate_collision_buildings": len(collisions), "excluded_buildings": len(excluded),
            },
            "selected_buildings": selected_records,
            "selection_mode": "offline_disjoint_population_core8_plus_e3_risk2_n10_v1.1",
            "version": VERSION, "vision_requests": 0,
        }
    finally:
        if e3 is not None: e3.close()
        e2.close()
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256, "e3_selection": e3_snapshot.byte_sha256,
        **{spec.name + "_manifest": spec.manifest_byte_sha256 for spec in prior_specs},
        **{spec.name + "_sidecar": spec.sidecar_byte_sha256 for spec in prior_specs},
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256({"domain": MANIFEST_DOMAIN, "manifest": body})
    return body


def validate_manifest(
    manifest_path: Path | str,
    *,
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    prior_specs: Iterable[PriorValidationSpec],
    expected_seed: str = DEFAULT_SAMPLE_SEED,
) -> ValidationResult:
    path = Path(manifest_path).resolve(); raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest(); checks: list[ValidationCheck] = []
    self_sha = None; buildings = 0; occurrences = 0
    try:
        payload = json.loads(raw.decode("utf-8"))
        canonical_raw = (canonical_json(payload) + "\n").encode("utf-8")
        checks.append(ValidationCheck("canonical_json_bytes", raw == canonical_raw, hashlib.sha256(canonical_raw).hexdigest(), byte_sha))
        if not isinstance(payload, dict): raise ValueError("manifest root must be object")
        self_sha = payload.get("semantic_coverage_manifest_sha256")
        buildings = len(payload.get("selected_buildings", [])); occurrences = int(payload.get("planned_occurrence_count", 0))
        expected = _replay(e2_spec, e3_spec, prior_specs, expected_seed)
        expected_raw = (canonical_json(expected) + "\n").encode("utf-8")
        checks.append(ValidationCheck("entire_canonical_manifest_byte_equality", raw == expected_raw, hashlib.sha256(expected_raw).hexdigest(), byte_sha))
        checks.extend((
            ValidationCheck("sample_seed", payload.get("sample_seed") == expected_seed, expected_seed, str(payload.get("sample_seed"))),
            ValidationCheck("building_count", buildings == EXPECTED_BUILDINGS, str(EXPECTED_BUILDINGS), str(buildings)),
            ValidationCheck("occurrence_count", occurrences == EXPECTED_OCCURRENCES, str(EXPECTED_OCCURRENCES), str(occurrences)),
            ValidationCheck("zero_requests", all(payload.get(key) == 0 for key in ("network_requests", "vision_requests", "llm_requests")), "network=vision=llm=0", canonical_json({key: payload.get(key) for key in ("network_requests", "vision_requests", "llm_requests")})),
            ValidationCheck("selection_not_result_conditioned", payload.get("config", {}).get("model_result_selection_used") is False, "false", str(payload.get("config", {}).get("model_result_selection_used"))),
        ))
    except Exception as exc:
        checks.append(ValidationCheck("independent_replay", False, "complete", f"{type(exc).__name__}: {exc}"))
    return ValidationResult(path, bool(checks) and all(value.passed for value in checks), tuple(checks), len(raw), byte_sha, None if self_sha is None else str(self_sha), buildings, occurrences)


__all__ = ["PriorValidationSpec", "ValidationCheck", "ValidationResult", "default_prior_specs", "validate_manifest"]
