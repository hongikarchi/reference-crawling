"""Immutable E2/E3 adapters and manifest builder for semantic coverage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    DEFAULT_SAMPLE_SEED,
    P2_POLICY_ID,
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
    SEMANTIC_COVERAGE_VERSION,
    BuildingInventoryItem,
    CoverageCandidate,
    GuardedSelection,
    select_building_coverage,
    select_guarded_n10,
)


DEFAULT_E2_RELATIVE_PATH = Path(
    "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
)
DEFAULT_E3_RELATIVE_PATH = Path(
    "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db"
)
DEFAULT_E2_SIZE = 10_164_682_752
DEFAULT_E2_SHA256 = "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
DEFAULT_E2_LOGICAL_SHA256 = (
    "795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc"
)
DEFAULT_E3_SIZE = 10_236_592_128
DEFAULT_E3_SHA256 = "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4"
DEFAULT_E3_LOGICAL_SHA256 = (
    "6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce"
)
DEFAULT_E2_RUN_ID = "e2-e61327cad29ba08b272febe3"
DEFAULT_E3_RUN_ID = "e3-full-d7263341bb074292b582ae17"
DEFAULT_E2_APPLICATION_ID = 1_160_923_461
DEFAULT_E3_APPLICATION_ID = 1_160_989_011
DEFAULT_USER_VERSION = 1


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    path: Path
    expected_size: int
    expected_sha256: str
    expected_logical_sha256: str
    expected_run_id: str
    expected_application_id: int
    expected_user_version: int = DEFAULT_USER_VERSION


@dataclass(frozen=True)
class ArtifactSnapshot:
    name: str
    path: Path
    size_bytes: int
    byte_sha256: str
    logical_sha256: str
    run_id: str
    contract_version: str
    builder_version: str
    application_id: int
    user_version: int

    def as_record(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "builder_version": self.builder_version,
            "byte_sha256": self.byte_sha256,
            "contract_version": self.contract_version,
            "logical_sha256": self.logical_sha256,
            "name": self.name,
            "path": str(self.path),
            "run_id": self.run_id,
            "size_bytes": self.size_bytes,
            "user_version": self.user_version,
        }


def default_specs(repo_root: Path | str) -> tuple[ArtifactSpec, ArtifactSpec]:
    root = Path(repo_root).resolve()
    return (
        ArtifactSpec(
            "e2_evidence",
            (root / DEFAULT_E2_RELATIVE_PATH).resolve(),
            DEFAULT_E2_SIZE,
            DEFAULT_E2_SHA256,
            DEFAULT_E2_LOGICAL_SHA256,
            DEFAULT_E2_RUN_ID,
            DEFAULT_E2_APPLICATION_ID,
        ),
        ArtifactSpec(
            "e3_selection",
            (root / DEFAULT_E3_RELATIVE_PATH).resolve(),
            DEFAULT_E3_SIZE,
            DEFAULT_E3_SHA256,
            DEFAULT_E3_LOGICAL_SHA256,
            DEFAULT_E3_RUN_ID,
            DEFAULT_E3_APPLICATION_ID,
        ),
    )


def sha256_file(path: Path | str, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_sidecars(path: Path | str) -> tuple[Path, ...]:
    value = Path(path)
    candidates = tuple(
        Path(str(value) + suffix)
        for suffix in ("-wal", "-shm", "-journal", ".lock")
    )
    return tuple(candidate for candidate in candidates if candidate.exists())


def open_immutable(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _logical_sha(connection: sqlite3.Connection, *, name: str, run_id: str) -> str:
    table = "e2_metrics" if name == "e2_evidence" else "selection_metrics"
    row = connection.execute(
        f"""
        SELECT value_text FROM {table}
        WHERE run_id=? AND phase='validation'
          AND metric_name='output_logical_sha256' AND stratum_json='{{}}'
        """,
        (run_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"{name} has no stored logical SHA-256")
    return str(row[0])


def inspect_artifact(spec: ArtifactSpec) -> tuple[ArtifactSnapshot, sqlite3.Connection]:
    path = spec.path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    sidecars = sqlite_sidecars(path)
    if sidecars:
        raise RuntimeError(f"SQLite input has sidecars: {[str(x) for x in sidecars]}")
    size = path.stat().st_size
    if size != spec.expected_size:
        raise ValueError(f"{spec.name} size mismatch: {size} != {spec.expected_size}")
    byte_sha = sha256_file(path)
    if byte_sha != spec.expected_sha256:
        raise ValueError(f"{spec.name} byte SHA mismatch: {byte_sha}")
    connection = open_immutable(path)
    table = "e2_runs" if spec.name == "e2_evidence" else "selection_runs"
    row = connection.execute(f"SELECT * FROM {table}").fetchall()
    if len(row) != 1:
        connection.close()
        raise ValueError(f"{spec.name} must contain exactly one run")
    run = row[0]
    run_id = str(run["run_id"])
    logical_sha = _logical_sha(connection, name=spec.name, run_id=run_id)
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    expected = {
        "run_id": (run_id, spec.expected_run_id),
        "logical_sha256": (logical_sha, spec.expected_logical_sha256),
        "application_id": (application_id, spec.expected_application_id),
        "user_version": (user_version, spec.expected_user_version),
        "status": (str(run["status"]), "complete"),
        "selection_mode": (str(run["selection_mode"]), "full"),
    }
    failures = {key: pair for key, pair in expected.items() if pair[0] != pair[1]}
    if failures:
        connection.close()
        raise ValueError(f"{spec.name} immutable contract mismatch: {failures!r}")
    if spec.name == "e3_selection":
        request_values = (
            int(run["network_requests"]),
            int(run["vision_requests"]),
            int(run["llm_requests"]),
        )
        if request_values != (0, 0, 0) or str(run["artifact_scope"]) != "candidate_only":
            connection.close()
            raise ValueError("E3 is not the frozen offline candidate-only artifact")
    snapshot = ArtifactSnapshot(
        name=spec.name,
        path=path,
        size_bytes=size,
        byte_sha256=byte_sha,
        logical_sha256=logical_sha,
        run_id=run_id,
        contract_version=str(run["contract_version"]),
        builder_version=str(run["builder_version"]),
        application_id=application_id,
        user_version=user_version,
    )
    return snapshot, connection


def _single_column_set(
    connection: sqlite3.Connection, sql: str, params: Iterable[Any] = ()
) -> set[str]:
    return {str(row[0]) for row in connection.execute(sql, tuple(params))}


def collect_inventory(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    enforce_production_counts: bool = True,
) -> tuple[tuple[BuildingInventoryItem, ...], dict[str, Any]]:
    """Collect eligible E3 buildings plus full-population accounting."""

    p1_changed = _single_column_set(
        connection,
        """
        SELECT a.selection_id
        FROM shortlist_items a JOIN shortlist_items b
          ON b.run_id=a.run_id AND b.selection_id=a.selection_id
        WHERE a.run_id=? AND a.policy_id='p0_editorial_baseline'
          AND b.policy_id='p1_quality_gated_editorial'
          AND a.shortlist_rank=1 AND b.shortlist_rank=1
          AND a.candidate_id<>b.candidate_id
        """,
        (run_id,),
    )
    p2_changed = _single_column_set(
        connection,
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
        """,
        (run_id, run_id, run_id, run_id),
    )
    qa_fallback = _single_column_set(
        connection,
        """
        SELECT DISTINCT selection_id FROM policy_rankings
        WHERE run_id=? AND policy_id='p1_quality_gated_editorial'
          AND qa_fallback=1
        """,
        (run_id,),
    )
    gallery_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT selection_id,
                   SUM(CASE WHEN instr(roles_json,'\"gallery\"')>0 THEN 1 ELSE 0 END)
            FROM image_candidates WHERE run_id=? GROUP BY selection_id
            """,
            (run_id,),
        )
    }
    items: list[BuildingInventoryItem] = []
    total = 0
    no_success = 0
    population_by_source_stratum: Counter[tuple[str, str]] = Counter()
    for row in connection.execute(
        "SELECT * FROM selected_buildings WHERE run_id=? ORDER BY selection_rank",
        (run_id,),
    ):
        total += 1
        detail = json.loads(str(row["detail_json"]))
        summary = detail["building_summary"]
        source = str(row["source"])
        stratum = str(summary["stratum"])
        population_by_source_stratum[(source, stratum)] += 1
        successful = int(summary["successful_asset_count"])
        if successful == 0:
            no_success += 1
            continue
        selection_id = str(row["selection_id"])
        item = BuildingInventoryItem(
            selection_id=selection_id,
            source=source,
            source_building_id=str(row["source_building_id"]),
            name=None if row["name"] is None else str(row["name"]),
            population_stratum=stratum,
            successful_asset_count=successful,
            successful_gallery_count=gallery_counts.get(selection_id, 0),
            source_record_sha256=str(row["e2_source_record_sha256"]),
            selection_record_sha256=str(row["selection_record_sha256"]),
            qa_fallback=selection_id in qa_fallback,
            p1_rank1_changed=selection_id in p1_changed,
            p2_top3_changed=selection_id in p2_changed,
            cross_source_candidate=bool(summary["cross_source_candidate"]),
        )
        items.append(item)
    inventory = tuple(items)
    by_source = Counter(item.source for item in inventory)
    overlay_by_source = {
        source: {
            "p1_rank1_changed": sum(
                item.source == source and item.p1_rank1_changed for item in inventory
            ),
            "p2_top3_changed": sum(
                item.source == source and item.p2_top3_changed for item in inventory
            ),
            "qa_fallback": sum(
                item.source == source and item.qa_fallback for item in inventory
            ),
        }
        for source in ("architizer", "divisare")
    }
    accounting = {
        "candidate_occurrences": int(
            connection.execute(
                "SELECT COUNT(*) FROM image_candidates WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "eligible_buildings": len(inventory),
        "eligible_by_source": dict(sorted(by_source.items())),
        "inventory_manifest_sha256": canonical_sha256(
            {
                "ordered_inventory": [
                    {
                        "record_sha256": item.record_sha256,
                        "selection_id": item.selection_id,
                    }
                    for item in inventory
                ],
                "version": SEMANTIC_COVERAGE_VERSION,
            }
        ),
        "no_success_buildings": no_success,
        "overlay_by_source": overlay_by_source,
        "population_by_source_stratum": [
            {"count": count, "source": source, "stratum": stratum}
            for (source, stratum), count in sorted(population_by_source_stratum.items())
        ],
        "source_total_buildings": total,
    }
    if enforce_production_counts:
        expected = {
            "source_total_buildings": 91_803,
            "eligible_buildings": 91_183,
            "no_success_buildings": 620,
            "candidate_occurrences": 1_429_581,
            "eligible_by_source": {"architizer": 61_351, "divisare": 29_832},
            "overlay_by_source": {
                "architizer": {
                    "p1_rank1_changed": 25,
                    "p2_top3_changed": 283,
                    "qa_fallback": 14,
                },
                "divisare": {
                    "p1_rank1_changed": 30,
                    "p2_top3_changed": 29,
                    "qa_fallback": 0,
                },
            },
        }
        mismatches = {
            key: (accounting[key], value)
            for key, value in expected.items()
            if accounting[key] != value
        }
        if mismatches:
            raise ValueError(f"E3 production population accounting mismatch: {mismatches!r}")
    return inventory, accounting


def _json_tuple(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("expected a JSON string list")
    return tuple(sorted(set(parsed)))


def load_building_candidates(
    e3: sqlite3.Connection,
    e2: sqlite3.Connection,
    *,
    e3_run_id: str,
    e2_run_id: str,
    building: BuildingInventoryItem,
) -> tuple[CoverageCandidate, ...]:
    shortlist_hashes = {
        str(row["candidate_id"]): str(row["item_record_sha256"])
        for row in e3.execute(
            """
            SELECT candidate_id,item_record_sha256 FROM shortlist_items
            WHERE run_id=? AND selection_id=? AND policy_id=?
            """,
            (e3_run_id, building.selection_id, P2_POLICY_ID),
        )
    }
    result: list[CoverageCandidate] = []
    rows = e3.execute(
        """
        SELECT c.*,r.editorial_rank,r.shortlist_rank,r.qa_fallback,r.hard_risk,
               r.ranking_record_sha256
        FROM image_candidates c JOIN policy_rankings r
          ON r.run_id=c.run_id AND r.selection_id=c.selection_id
         AND r.candidate_id=c.candidate_id
        WHERE c.run_id=? AND c.selection_id=? AND r.policy_id=?
        ORDER BY r.editorial_rank,c.candidate_id
        """,
        (e3_run_id, building.selection_id, P2_POLICY_ID),
    )
    for row in rows:
        source = str(row["source"])
        asset_id = str(row["source_asset_id"])
        asset = e2.execute(
            "SELECT * FROM assets WHERE run_id=? AND source=? AND source_asset_id=?",
            (e2_run_id, source, asset_id),
        ).fetchall()
        relation = e2.execute(
            """
            SELECT relation_record_sha256 FROM building_assets
            WHERE run_id=? AND source=? AND source_building_id=? AND source_asset_id=?
            """,
            (e2_run_id, source, building.source_building_id, asset_id),
        ).fetchall()
        if len(asset) != 1 or len(relation) != 1:
            raise ValueError(
                f"E3 candidate lacks a unique E2 asset/building relation: {row['candidate_id']}"
            )
        e2row = asset[0]
        e2_provenance = json.loads(str(e2row["provenance_json"]))
        e2_quality_flags = tuple(
            sorted(set(str(value) for value in e2_provenance.get("quality_flags", [])))
        )
        e3_quality_flags = _json_tuple(str(row["quality_flags_json"]))
        e2_roles = _json_tuple(
            str(
                e2.execute(
                    """
                    SELECT roles_json FROM building_assets
                    WHERE run_id=? AND source=? AND source_building_id=?
                      AND source_asset_id=?
                    """,
                    (e2_run_id, source, building.source_building_id, asset_id),
                ).fetchone()[0]
            )
        )
        e3_roles = _json_tuple(str(row["roles_json"]))
        expected_equal = {
            "source_record_sha256": (
                str(row["source_record_sha256"]), str(e2row["source_record_sha256"])
            ),
            "normalized_pixel_sha256": (
                str(row["normalized_pixel_sha256"]),
                str(e2row["normalized_pixel_sha256"]),
            ),
            "building_relation_record_sha256": (
                str(row["building_relation_record_sha256"]), str(relation[0][0])
            ),
            "canonical_url": (str(row["canonical_url"]), str(e2row["canonical_url"])),
            "fetch_url": (str(row["fetch_url"]), str(e2row["fetch_url"])),
            "final_url": (row["final_url"], e2row["final_url"]),
            "original_width": (row["original_width"], e2row["original_width"]),
            "original_height": (row["original_height"], e2row["original_height"]),
            "normalized_width": (row["normalized_width"], e2row["normalized_width"]),
            "normalized_height": (row["normalized_height"], e2row["normalized_height"]),
            "quality_flags": (e3_quality_flags, e2_quality_flags),
            "building_roles": (e3_roles, e2_roles),
        }
        mismatches = {key: pair for key, pair in expected_equal.items() if pair[0] != pair[1]}
        if mismatches:
            raise ValueError(f"E3/E2 candidate lineage mismatch: {mismatches!r}")
        if str(e2row["fingerprint_status"]) != "success":
            raise ValueError("E3 candidate maps to a non-success E2 asset")
        detail = json.loads(str(row["detail_json"]))
        phash_hex = str(detail["phash_hex"])
        if phash_hex != str(e2row["phash_hex"]):
            raise ValueError("E3/E2 pHash mismatch")
        raw_sha = e2row["raw_response_sha256"]
        if raw_sha is None:
            raise ValueError("successful E2 asset has no raw response SHA")
        result.append(
            CoverageCandidate(
                candidate_id=str(row["candidate_id"]),
                selection_id=building.selection_id,
                source=source,
                source_building_id=building.source_building_id,
                source_asset_id=asset_id,
                editorial_rank=int(row["editorial_rank"]),
                p2_shortlist_rank=(
                    None if row["shortlist_rank"] is None else int(row["shortlist_rank"])
                ),
                qa_fallback=bool(row["qa_fallback"]),
                hard_risk=bool(row["hard_risk"]),
                roles=e3_roles,
                source_ordinal=(
                    None if row["source_ordinal"] is None else int(row["source_ordinal"])
                ),
                canonical_url=str(row["canonical_url"]),
                fetch_url=str(e2row["fetch_url"]),
                final_url=None if e2row["final_url"] is None else str(e2row["final_url"]),
                original_width=(
                    None if row["original_width"] is None else int(row["original_width"])
                ),
                original_height=(
                    None if row["original_height"] is None else int(row["original_height"])
                ),
                normalized_width=(
                    None if row["normalized_width"] is None else int(row["normalized_width"])
                ),
                normalized_height=(
                    None if row["normalized_height"] is None else int(row["normalized_height"])
                ),
                quality_flags=e3_quality_flags,
                normalized_pixel_sha256=str(row["normalized_pixel_sha256"]),
                phash_node_id=str(row["phash_node_id"]),
                phash_hex=phash_hex,
                raw_response_sha256=str(raw_sha),
                e3_source_record_sha256=str(row["source_record_sha256"]),
                e3_candidate_record_sha256=str(row["candidate_record_sha256"]),
                e3_ranking_record_sha256=str(row["ranking_record_sha256"]),
                e3_shortlist_item_record_sha256=shortlist_hashes.get(
                    str(row["candidate_id"])
                ),
                e2_asset_record_sha256=str(e2row["source_record_sha256"]),
                e2_building_relation_record_sha256=str(relation[0][0]),
            )
        )
    if len(result) != building.successful_asset_count:
        raise ValueError(
            f"candidate count mismatch for {building.selection_id}: "
            f"{len(result)} != {building.successful_asset_count}"
        )
    return tuple(result)


def _input_rehash(spec: ArtifactSpec, before: ArtifactSnapshot) -> ArtifactSnapshot:
    if sqlite_sidecars(spec.path):
        raise RuntimeError(f"SQLite input gained sidecars: {spec.path}")
    after_size = spec.path.stat().st_size
    after_sha = sha256_file(spec.path)
    if after_size != before.size_bytes or after_sha != before.byte_sha256:
        raise RuntimeError(f"immutable input changed during planning: {spec.name}")
    return before


def build_semantic_coverage_manifest(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    *,
    seed: str = DEFAULT_SAMPLE_SEED,
    enforce_production_counts: bool = True,
) -> dict[str, Any]:
    """Build the canonical, offline N10 planning manifest in memory."""

    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3_snapshot: ArtifactSnapshot | None = None
    e3: sqlite3.Connection | None = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        e3_input = e3.execute(
            "SELECT * FROM selection_inputs WHERE run_id=? AND input_role='e2_evidence'",
            (e3_snapshot.run_id,),
        ).fetchall()
        if len(e3_input) != 1:
            raise ValueError("E3 does not bind exactly one E2 evidence input")
        bound = e3_input[0]
        if (
            int(bound["size_bytes"]) != e2_snapshot.size_bytes
            or str(bound["sha256_before"]) != e2_snapshot.byte_sha256
            or str(bound["sha256_after"]) != e2_snapshot.byte_sha256
            or str(bound["logical_sha256"]) != e2_snapshot.logical_sha256
        ):
            raise ValueError("E3's stored E2 lineage does not match the supplied E2")

        inventory, population = collect_inventory(
            e3,
            run_id=e3_snapshot.run_id,
            enforce_production_counts=enforce_production_counts,
        )
        guarded = select_guarded_n10(inventory, seed=seed)
        selected_records: list[dict[str, Any]] = []
        occurrence_hashes: list[dict[str, Any]] = []
        pixel_occurrences: Counter[str] = Counter()
        for selection in guarded:
            candidates = load_building_candidates(
                e3,
                e2,
                e3_run_id=e3_snapshot.run_id,
                e2_run_id=e2_snapshot.run_id,
                building=selection.building,
            )
            plan = select_building_coverage(candidates)
            selection_record = selection.as_record()
            combined = {
                **selection_record,
                "coverage_plan": plan.as_record(),
                "coverage_plan_record_sha256": plan.record_sha256,
            }
            combined_sha = canonical_sha256(combined)
            selected_records.append(
                {"selected_building": combined, "selected_building_record_sha256": combined_sha}
            )
            for occurrence in plan.selected_occurrences:
                pixel = occurrence.candidate.normalized_pixel_sha256
                pixel_occurrences[pixel] += 1
                occurrence_hashes.append(
                    {
                        "candidate_id": occurrence.candidate.candidate_id,
                        "occurrence_record_sha256": occurrence.record_sha256,
                        "selection_id": selection.building.selection_id,
                    }
                )

        config = {
            "anchor_policy_id": P2_POLICY_ID,
            "anchor_ranks": [1, 2, 3],
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "gallery_probe_slots": ["gallery_early", "gallery_middle", "gallery_late"],
            "hard_risk_fallback_only_when_all_candidates_risky": True,
            "max_occurrences_per_building": 6,
            "phash_semantic_reuse_allowed": False,
            "phash_transitive_closure_allowed": False,
            "probe_redundancy_order": [
                "exact_normalized_pixel",
                "identical_phash",
                "direct_phash_le8",
            ],
            "sample_guard_order": [selection.guard_name for selection in guarded],
            "selection_version": SEMANTIC_COVERAGE_VERSION,
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
            "llm_requests": 0,
            "network_requests": 0,
            "ordered_building_manifest_sha256": canonical_sha256(
                {
                    "ordered_buildings": [
                        {
                            "rank": index,
                            "record_sha256": value["selected_building_record_sha256"],
                        }
                        for index, value in enumerate(selected_records, 1)
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
            "planned_e1_exact_duplicate_savings": sum(pixel_occurrences.values())
            - len(pixel_occurrences),
            "planned_occurrence_count": sum(pixel_occurrences.values()),
            "planned_unique_e1_pixel_count": len(pixel_occurrences),
            "population": population,
            "sample_seed": seed,
            "sample_size_buildings": len(guarded),
            "selected_buildings": selected_records,
            "selection_mode": "offline_semantic_coverage_n10_plan",
            "version": SEMANTIC_COVERAGE_VERSION,
            "vision_requests": 0,
        }
    finally:
        if e3 is not None:
            e3.close()
        e2.close()
    _input_rehash(e2_spec, e2_snapshot)
    if e3_snapshot is None:
        raise AssertionError("E3 snapshot was not created")
    _input_rehash(e3_spec, e3_snapshot)
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256,
        "e3_selection": e3_snapshot.byte_sha256,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": body}
    )
    return body


def write_semantic_coverage_manifest(
    path: Path | str, manifest: dict[str, Any]
) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create is the no-clobber boundary.  A partial file is retained
    # if the underlying write fails so a later operator cannot mistake it for
    # a completed, canonical manifest.
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(manifest))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return output


__all__ = [
    "ArtifactSnapshot",
    "ArtifactSpec",
    "DEFAULT_E2_APPLICATION_ID",
    "DEFAULT_E2_LOGICAL_SHA256",
    "DEFAULT_E2_RELATIVE_PATH",
    "DEFAULT_E2_RUN_ID",
    "DEFAULT_E2_SHA256",
    "DEFAULT_E2_SIZE",
    "DEFAULT_E3_APPLICATION_ID",
    "DEFAULT_E3_LOGICAL_SHA256",
    "DEFAULT_E3_RELATIVE_PATH",
    "DEFAULT_E3_RUN_ID",
    "DEFAULT_E3_SHA256",
    "DEFAULT_E3_SIZE",
    "build_semantic_coverage_manifest",
    "collect_inventory",
    "default_specs",
    "inspect_artifact",
    "load_building_candidates",
    "open_immutable",
    "sha256_file",
    "sqlite_sidecars",
    "write_semantic_coverage_manifest",
]
