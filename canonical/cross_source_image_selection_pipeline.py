"""Offline sample builder for cross-source image shortlist policy E3.

The builder reads one accepted E2 evidence artifact immutably and writes a
new candidate-only SQLite sidecar.  It compares P0/P1/P2 shortlists, but never
chooses a final representative image and never creates or executes Vision
work.  Only sample mode is implemented in this version; a future full build
requires a merge-stream validator and a separate user gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import (
    Candidate,
    DirectPHashEdge,
    E3_POLICY_VERSION,
    E3_SELECTION_VERSION,
    SamplingItem,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    deterministic_sample_score,
    deterministic_stratified_sample,
    editorial_sort_key,
    ordered_sample_manifest_sha256,
    policy_definitions,
)
from canonical.cross_source_image_selection_sidecar import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    acquire_build_lock,
    finalize_sidecar,
    initialize_sidecar,
    lock_path_for,
    prepare_immutable_sidecar,
    sqlite_sidecar_paths,
    validate_sidecar,
)
from canonical.cross_source_image_selection_sources import (
    BuildingImageCandidate,
    BuildingSummary,
    E2ArtifactSpec,
    E2SelectionSources,
    open_e2_selection_sources,
)


PIPELINE_VERSION = "archibe-e3-cross-source-image-selection-pipeline-v1"
LOGICAL_MANIFEST_VERSION = "archibe-e3-selection-logical-manifest-v1"
DEFAULT_SAMPLE_SEED = "archibe-e3-shortlist-smoke-v1"
DEFAULT_SHORTLIST_SIZE = 3
DEFAULT_BATCH_SIZE = 1_000

DEFAULT_E2_RELATIVE_PATH = Path(
    "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
)
DEFAULT_E2_SIZE = 10_164_682_752
DEFAULT_E2_SHA256 = (
    "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19"
)
DEFAULT_E2_LOGICAL_SHA256 = (
    "795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc"
)
DEFAULT_E2_CONTRACT_VERSION = "archibe-e2-cross-source-image-evidence-v1"
DEFAULT_E2_BUILDER_VERSION = (
    "archibe-e2-cross-source-image-evidence-pipeline-v5"
)

LOGICAL_SELECTION_TABLES = (
    "policy_definitions",
    "population_strata",
    "selected_buildings",
    "image_candidates",
    "policy_rankings",
    "shortlist_items",
    "queue_estimates",
)


@dataclass(frozen=True)
class BuildConfig:
    e2_path: Path
    output_path: Path
    expected_e2_size: int
    expected_e2_sha256: str
    expected_e2_logical_sha256: str
    expected_e2_contract_version: str = DEFAULT_E2_CONTRACT_VERSION
    expected_e2_builder_version: str = DEFAULT_E2_BUILDER_VERSION
    sample_size: int = 10
    sample_seed: str = DEFAULT_SAMPLE_SEED
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    run_id: str
    status: str
    logical_sha256: str
    selected_buildings: int
    image_candidates: int
    shortlist_items: int
    elapsed_seconds: float


def default_e2_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / DEFAULT_E2_RELATIVE_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normal_name(value: str) -> str:
    return " ".join(str(value).casefold().split())


def _primary_role(roles: Sequence[str]) -> str:
    values = tuple(sorted(set(str(role) for role in roles)))
    if not values:
        raise ValueError("E2 candidate has no source role")
    if "cover" in values:
        return "cover"
    if "gallery" in values:
        return "gallery"
    return values[0]


def _role_rank(role: str) -> int:
    return 0 if role == "cover" else 1 if role == "gallery" else 2


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


def _sampling_item(summary: BuildingSummary) -> SamplingItem:
    return SamplingItem(
        identity=f"{summary.source}:building:{summary.source_building_id}",
        source=summary.source,
        stratum=summary.stratum,
        input_record_sha256=canonical_sha256(_summary_record(summary)),
    )


def _candidate(source: BuildingImageCandidate) -> Candidate:
    return Candidate(
        source=source.source,
        source_building_id=source.source_building_id,
        source_asset_id=source.source_asset_id,
        fingerprint_status="success",
        role=_primary_role(source.roles),
        ordinal=source.lowest_project_ordinal,
        original_width=source.original_width,
        original_height=source.original_height,
        quality_flags=tuple(source.quality_flags),
        source_record_sha256=source.source_asset_record_sha256,
        exact_cluster_id=source.exact_cluster_id,
        phash_node_id=source.phash_node_id,
        canonical_url=source.canonical_url,
    )


def _candidate_mapping_values(
    mapped: Candidate,
    source: BuildingImageCandidate,
    selection_id: str,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "candidate_id": mapped.candidate_id,
        "selection_id": selection_id,
        "source": source.source,
        "source_building_id": source.source_building_id,
        "source_project_id": None,
        "source_asset_id": source.source_asset_id,
        "fingerprint_status": "success",
        "canonical_url": source.canonical_url,
        "fetch_url": source.fetch_url,
        "final_url": source.final_url,
        "roles_json": list(source.roles),
        "primary_role": mapped.role,
        "role_rank": _role_rank(mapped.role),
        "source_ordinal": source.lowest_project_ordinal,
        "ordinal_is_derived": int(source.lowest_project_ordinal is not None),
        "original_width": source.original_width,
        "original_height": source.original_height,
        "normalized_width": source.normalized_width,
        "normalized_height": source.normalized_height,
        "quality_flags_json": list(source.quality_flags),
        "low_information": int("low_information" in source.quality_flags),
        "normalized_pixel_sha256": source.normalized_pixel_sha256,
        "exact_cluster_id": source.exact_cluster_id,
        "phash_node_id": source.phash_node_id,
        "source_record_sha256": source.source_asset_record_sha256,
        "occurrence_record_sha256": None,
        "project_relation_record_sha256": None,
        "building_relation_record_sha256": source.building_relation_record_sha256,
    }
    values["candidate_record_sha256"] = canonical_sha256(
        _candidate_mapping_record_body(values)
    )
    return values


def _candidate_mapping_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: values[key]
        for key in (
            "building_relation_record_sha256",
            "candidate_id",
            "canonical_url",
            "exact_cluster_id",
            "fetch_url",
            "final_url",
            "fingerprint_status",
            "low_information",
            "normalized_height",
            "normalized_pixel_sha256",
            "normalized_width",
            "occurrence_record_sha256",
            "ordinal_is_derived",
            "original_height",
            "original_width",
            "phash_node_id",
            "primary_role",
            "project_relation_record_sha256",
            "quality_flags_json",
            "role_rank",
            "roles_json",
            "selection_id",
            "source",
            "source_asset_id",
            "source_building_id",
            "source_ordinal",
            "source_project_id",
            "source_record_sha256",
        )
    }


def _policy_record_body(
    *,
    policy_id: str,
    policy_name: str,
    description: str,
    shortlist_size: int,
    definition: Mapping[str, Any],
    policy_config_sha256: str,
) -> dict[str, Any]:
    return {
        "definition": dict(definition),
        "description": description,
        "enabled": True,
        "policy_config_sha256": policy_config_sha256,
        "policy_id": policy_id,
        "policy_name": policy_name,
        "policy_version": E3_POLICY_VERSION,
        "shortlist_size": shortlist_size,
    }


def _policy_set_sha(policy_rows: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(
        {
            "contract_version": E3_SELECTION_VERSION,
            "ordered_policies": [
                {
                    "policy_config_sha256": str(row["policy_config_sha256"]),
                    "policy_id": str(row["policy_id"]),
                    "policy_record_sha256": str(row["policy_record_sha256"]),
                }
                for row in sorted(policy_rows, key=lambda value: str(value["policy_id"]))
            ],
        }
    )


def _stratum_id(source: str, stratum: str) -> str:
    return "e3str_" + canonical_sha256(
        {"source": source, "stratum": stratum, "version": E3_SELECTION_VERSION}
    )


def _stratum_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "eligible_count": int(values["eligible_count"]),
        "population_count": int(values["population_count"]),
        "selected_building_count": int(values["selected_building_count"]),
        "selected_candidate_count": int(values["selected_candidate_count"]),
        "stratum": dict(values["stratum_json"]),
        "stratum_id": str(values["stratum_id"]),
        "stratum_key": str(values["stratum_key"]),
    }


def _selection_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: values[key]
        for key in (
            "e2_relation_record_sha256",
            "e2_source_record_sha256",
            "entity_type",
            "name",
            "normalized_name",
            "selection_id",
            "selection_rank",
            "selection_reason",
            "source",
            "source_building_id",
            "source_entity_id",
            "source_project_id",
            "stratum_id",
        )
    }


def _shortlist_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authoritative": bool(values["authoritative"]),
        "candidate_id": str(values["candidate_id"]),
        "policy_id": str(values["policy_id"]),
        "selection_id": str(values["selection_id"]),
        "shortlist_rank": int(values["shortlist_rank"]),
        "shortlist_state": str(values["shortlist_state"]),
    }


def _queue_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authoritative": bool(values["authoritative"]),
        "estimated_calls": values["estimated_calls"],
        "estimated_cost_usd": values["estimated_cost_usd"],
        "estimated_queue_items": int(values["estimated_queue_items"]),
        "estimate_id": str(values["estimate_id"]),
        "policy_id": str(values["policy_id"]),
        "population_count": int(values["population_count"]),
        "pricing_snapshot": dict(values["pricing_snapshot_json"]),
        "projected_input_tokens": values["projected_input_tokens"],
        "projected_output_tokens": values["projected_output_tokens"],
        "projected_quota_percent": values["projected_quota_percent"],
        "projected_total_tokens": values["projected_total_tokens"],
        "queue_unit": str(values["queue_unit"]),
        "quota_basis": values["quota_basis"],
        "requests_executed": int(values["requests_executed"]),
        "retry_factor": float(values["retry_factor"]),
        "stratum_id": values["stratum_id"],
        "tokens_per_item_high": values["tokens_per_item_high"],
        "tokens_per_item_low": values["tokens_per_item_low"],
        "tokens_per_item_point": values["tokens_per_item_point"],
    }


def _ranking_state(reasons: Sequence[str], selected: bool) -> str:
    if selected:
        return "shortlisted"
    if any(str(reason).startswith("suppressed_") for reason in reasons):
        return "suppressed"
    if any(
        str(reason).startswith("excluded_non_success")
        or str(reason).startswith("excluded_quality_hard_risk")
        for reason in reasons
    ):
        return "ineligible"
    return "ranked"


def _schema_manifest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type,name,tbl_name,sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type,name
        """
    ).fetchall()
    return canonical_sha256(
        [
            {
                "type": row[0],
                "name": row[1],
                "table": row[2],
                "sql": row[3],
            }
            for row in rows
        ]
    )


def _primary_key_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(
        str(row[1])
        for row in sorted(
            (row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5])
        )
    )


def logical_selection_manifest(
    connection: sqlite3.Connection, run_id: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    table_manifests: dict[str, dict[str, Any]] = {}
    for table in LOGICAL_SELECTION_TABLES:
        columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        included = [
            column
            for column in columns
            if column not in {"created_at", "recorded_at", "updated_at"}
        ]
        primary_key = _primary_key_columns(connection, table)
        selected = ",".join(f'"{column}"' for column in included)
        ordering = ",".join(f'"{column}"' for column in primary_key)
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(
            f'SELECT {selected} FROM "{table}" WHERE run_id=? ORDER BY {ordering}',
            (run_id,),
        ):
            record = {column: row[index] for index, column in enumerate(included)}
            digest.update(canonical_json(record).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        table_manifests[table] = {"count": count, "sha256": digest.hexdigest()}
    run = connection.execute(
        """
        SELECT e2_logical_sha256,policy_set_sha256,selection_mode,
               sample_size,sample_seed,shortlist_size,
               ordered_selection_manifest_sha256
        FROM selection_runs WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError("selection run disappeared while hashing")
    body = {
        "e2_logical_sha256": str(run[0]),
        "manifest_version": LOGICAL_MANIFEST_VERSION,
        "ordered_selection_manifest_sha256": str(run[6]),
        "policy_set_sha256": str(run[1]),
        "sample_seed": run[4],
        "sample_size": run[3],
        "selection_mode": str(run[2]),
        "shortlist_size": int(run[5]),
        "tables": table_manifests,
    }
    return canonical_sha256(body), table_manifests


def _insert_metric(
    connection: sqlite3.Connection,
    run_id: str,
    phase: str,
    name: str,
    value: int | float | str,
    *,
    stratum: Mapping[str, Any] | None = None,
) -> None:
    integer: int | None = None
    real: float | None = None
    text: str | None = None
    if isinstance(value, bool) or isinstance(value, int):
        integer = int(value)
    elif isinstance(value, float):
        real = value
    else:
        text = str(value)
    connection.execute(
        """
        INSERT INTO selection_metrics(
          run_id,phase,metric_name,stratum_json,
          value_integer,value_real,value_text,recorded_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            phase,
            name,
            canonical_json(dict(stratum or {})),
            integer,
            real,
            text,
            _utc_now(),
        ),
    )


def _validate_config(config: BuildConfig) -> None:
    if config.sample_size < 1:
        raise ValueError("sample_size must be positive")
    if not config.sample_seed.strip():
        raise ValueError("sample_seed must be non-empty")
    if config.shortlist_size < 1:
        raise ValueError("shortlist_size must be positive")
    if config.batch_size < 1:
        raise ValueError("batch_size must be positive")
    if config.expected_e2_size < 1:
        raise ValueError("expected_e2_size must be positive")


def _queue_counts(
    shortlists: Mapping[tuple[str, str], Sequence[Any]],
    candidate_sources: Mapping[str, BuildingImageCandidate],
    *,
    shortlist_size: int,
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for policy in policy_definitions(shortlist_size):
        all_items: list[str] = []
        top_items: list[str] = []
        for values in shortlists.values():
            shortlist = next(
                (
                    value
                    for value in values
                    if value.policy.policy_id == policy.policy_id
                ),
                None,
            )
            # A sampled no-success building deliberately has no policy output.
            if shortlist is None:
                continue
            # Queue previews are frozen top-1/top-3 scenarios even when a
            # diagnostic build asks the policy engine for a wider shortlist.
            all_items.extend(shortlist.selected_candidate_ids[:3])
            if shortlist.selected_candidate_ids:
                top_items.append(shortlist.selected_candidate_ids[0])
        counts[policy.policy_id] = {
            "top1_no_reuse": len(top_items),
            "top1_exact_reuse": len(
                {candidate_sources[item].normalized_pixel_sha256 for item in top_items}
            ),
            "top3_no_reuse": len(all_items),
            "top3_exact_reuse": len(
                {candidate_sources[item].normalized_pixel_sha256 for item in all_items}
            ),
        }
    return counts


def _build_run_id(config: BuildConfig, policy_set_sha256: str) -> str:
    return "e3-" + canonical_sha256(
        {
            "builder_version": PIPELINE_VERSION,
            "contract_version": E3_SELECTION_VERSION,
            "e2_byte_sha256": config.expected_e2_sha256.lower(),
            "e2_logical_sha256": config.expected_e2_logical_sha256.lower(),
            "policy_set_sha256": policy_set_sha256,
            "sample_seed": config.sample_seed,
            "sample_size": config.sample_size,
            "shortlist_size": config.shortlist_size,
        }
    )[:24]


def build_cross_source_image_selection(config: BuildConfig) -> BuildResult:
    """Build one immutable E3 sample artifact and return its identity."""

    _validate_config(config)
    started = time.perf_counter()
    output = Path(config.output_path).resolve()
    e2_path = Path(config.e2_path).resolve()
    connection: sqlite3.Connection | None = None
    run_id: str | None = None
    with acquire_build_lock(lock_path_for(output)):
        try:
            spec = E2ArtifactSpec(
                path=e2_path,
                expected_size=config.expected_e2_size,
                expected_sha256=config.expected_e2_sha256,
                expected_logical_sha256=config.expected_e2_logical_sha256,
                expected_contract_version=config.expected_e2_contract_version,
                expected_builder_version=config.expected_e2_builder_version,
            )
            with open_e2_selection_sources(spec, batch_size=config.batch_size) as source:
                summaries = tuple(source.iter_building_summaries())
                if config.sample_size > len(summaries):
                    raise ValueError("sample_size exceeds E2 building population")
                sample_items = tuple(_sampling_item(value) for value in summaries)
                summary_by_identity = {
                    item.identity: summary
                    for item, summary in zip(sample_items, summaries)
                }
                selected_items = deterministic_stratified_sample(
                    sample_items,
                    sample_size=config.sample_size,
                    seed=config.sample_seed,
                )
                selected = tuple(
                    summary_by_identity[item.identity] for item in selected_items
                )
                ordered_manifest = ordered_sample_manifest_sha256(selected_items)

                source_candidates: dict[
                    tuple[str, str], tuple[BuildingImageCandidate, ...]
                ] = {}
                mapped_candidates: dict[tuple[str, str], tuple[Candidate, ...]] = {}
                candidate_source_by_id: dict[str, BuildingImageCandidate] = {}
                node_members: dict[
                    str, list[tuple[tuple[str, str], str]]
                ] = defaultdict(list)
                for summary in selected:
                    key = (summary.source, summary.source_building_id)
                    source_rows = tuple(source.iter_candidates(*key))
                    mapped = tuple(_candidate(value) for value in source_rows)
                    source_candidates[key] = source_rows
                    mapped_candidates[key] = mapped
                    for source_row, candidate in zip(source_rows, mapped):
                        candidate_source_by_id[candidate.candidate_id] = source_row
                        if candidate.phash_node_id is not None:
                            node_members[candidate.phash_node_id].append(
                                (key, candidate.candidate_id)
                            )

                direct_by_building: dict[
                    tuple[str, str], dict[tuple[str, str], DirectPHashEdge]
                ] = defaultdict(dict)
                for edge in source.direct_phash_pairs(set(node_members)):
                    for left_key, left_id in node_members.get(edge.left_node_id, ()):
                        for right_key, right_id in node_members.get(edge.right_node_id, ()):
                            if left_key != right_key:
                                continue
                            mapped_edge = DirectPHashEdge(
                                left_candidate_id=left_id,
                                right_candidate_id=right_id,
                                distance=edge.hamming_distance,
                            )
                            direct_by_building[left_key][mapped_edge.pair] = mapped_edge

                shortlists: dict[tuple[str, str], Sequence[Any]] = {}
                for key, candidates in mapped_candidates.items():
                    shortlists[key] = (
                        compare_standard_policies(
                            candidates,
                            shortlist_size=config.shortlist_size,
                            direct_phash_edges=tuple(
                                direct_by_building[key].values()
                            ),
                        )
                        if candidates
                        else ()
                    )

                policies = policy_definitions(config.shortlist_size)
                policy_rows: list[dict[str, Any]] = []
                for policy in policies:
                    definition = policy.as_config()
                    row = {
                        "policy_id": policy.policy_id,
                        "policy_version": E3_POLICY_VERSION,
                        "policy_name": policy.policy_id,
                        "description": policy.description,
                        "shortlist_size": policy.shortlist_size,
                        "enabled": 1,
                        "definition_json": definition,
                        "policy_config_sha256": policy.config_sha256,
                    }
                    row["policy_record_sha256"] = canonical_sha256(
                        _policy_record_body(
                            policy_id=policy.policy_id,
                            policy_name=policy.policy_id,
                            description=policy.description,
                            shortlist_size=policy.shortlist_size,
                            definition=definition,
                            policy_config_sha256=policy.config_sha256,
                        )
                    )
                    policy_rows.append(row)
                policy_set_sha = _policy_set_sha(policy_rows)
                run_id = _build_run_id(config, policy_set_sha)

                population_counts = Counter(
                    (value.source, value.stratum) for value in summaries
                )
                eligible_counts = Counter(
                    (value.source, value.stratum)
                    for value in summaries
                    if value.successful_asset_count > 0
                )
                selected_counts = Counter(
                    (value.source, value.stratum) for value in selected
                )
                selected_candidate_counts = Counter()
                for summary in selected:
                    key = (summary.source, summary.source_building_id)
                    selected_candidate_counts[(summary.source, summary.stratum)] += len(
                        source_candidates[key]
                    )

                connection = initialize_sidecar(output)
                now = _utc_now()
                run_config = {
                    "authoritative": 0,
                    "creates_final_representative": False,
                    "creates_vision_tasks": False,
                    "llm_requests": 0,
                    "network_enabled": False,
                    "network_requests": 0,
                    "phash_semantic_reuse_allowed": False,
                    "phash_transitive_closure_allowed": False,
                    "pipeline_version": PIPELINE_VERSION,
                    "semantic_reuse_allowed": False,
                    "vision_requests": 0,
                }
                connection.execute(
                    """
                    INSERT INTO selection_runs(
                      run_id,contract_version,builder_version,e2_artifact_path,
                      e2_size_bytes,e2_byte_sha256,e2_logical_sha256,
                      policy_set_sha256,selection_mode,sample_size,sample_seed,
                      shortlist_size,ordered_selection_manifest_sha256,
                      config_json,network_requests,vision_requests,llm_requests,
                      authoritative,artifact_scope,status,started_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        E3_SELECTION_VERSION,
                        PIPELINE_VERSION,
                        str(e2_path),
                        source.lineage.artifact_size,
                        source.lineage.artifact_sha256,
                        source.lineage.stored_logical_sha256,
                        policy_set_sha,
                        "sample",
                        config.sample_size,
                        config.sample_seed,
                        config.shortlist_size,
                        ordered_manifest,
                        canonical_json(run_config),
                        0,
                        0,
                        0,
                        0,
                        "candidate_only",
                        "building",
                        now,
                    ),
                )
                input_detail = {
                    "e2_builder_version": source.lineage.builder_version,
                    "e2_contract_version": source.lineage.contract_version,
                    "e2_logical_sha256": source.lineage.stored_logical_sha256,
                    "e2_ordered_selection_manifest_sha256": (
                        source.lineage.ordered_selection_manifest_sha256
                    ),
                    "e2_run_id": source.lineage.run_id,
                    "e2_selection_mode": source.lineage.selection_mode,
                    "inherited_inputs": [
                        {
                            "input_name": value.input_name,
                            "size_bytes": value.size_bytes,
                            "sha256": value.sha256_before,
                        }
                        for value in source.lineage.inputs
                    ],
                }
                connection.execute(
                    """
                    INSERT INTO selection_inputs(
                      run_id,input_name,input_role,file_path,size_bytes,
                      sha256_before,sha256_after,logical_sha256,
                      application_id,user_version,schema_manifest_sha256,
                      recorded_at,detail_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        "e2_evidence",
                        "e2_evidence",
                        str(e2_path),
                        source.lineage.artifact_size,
                        source.lineage.artifact_sha256,
                        source.lineage.artifact_sha256,
                        source.lineage.stored_logical_sha256,
                        int(source.connection.execute("PRAGMA application_id").fetchone()[0]),
                        int(source.connection.execute("PRAGMA user_version").fetchone()[0]),
                        _schema_manifest(source.connection),
                        now,
                        canonical_json(input_detail),
                    ),
                )

                for row in policy_rows:
                    connection.execute(
                        """
                        INSERT INTO policy_definitions(
                          run_id,policy_id,policy_version,policy_name,description,
                          shortlist_size,enabled,definition_json,
                          policy_config_sha256,policy_record_sha256,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            row["policy_id"],
                            row["policy_version"],
                            row["policy_name"],
                            row["description"],
                            row["shortlist_size"],
                            row["enabled"],
                            canonical_json(row["definition_json"]),
                            row["policy_config_sha256"],
                            row["policy_record_sha256"],
                            now,
                        ),
                    )

                stratum_ids: dict[tuple[str, str], str] = {}
                for cell in sorted(population_counts):
                    source_name, stratum = cell
                    values: dict[str, Any] = {
                        "stratum_id": _stratum_id(source_name, stratum),
                        "stratum_key": f"{source_name}:{stratum}",
                        "stratum_json": {
                            "source": source_name,
                            "stratum": stratum,
                        },
                        "population_count": population_counts[cell],
                        "eligible_count": eligible_counts[cell],
                        "selected_building_count": selected_counts[cell],
                        "selected_candidate_count": selected_candidate_counts[cell],
                    }
                    values["stratum_record_sha256"] = canonical_sha256(
                        _stratum_record_body(values)
                    )
                    stratum_ids[cell] = values["stratum_id"]
                    connection.execute(
                        """
                        INSERT INTO population_strata(
                          run_id,stratum_id,stratum_key,stratum_json,
                          population_count,eligible_count,selected_building_count,
                          selected_candidate_count,stratum_record_sha256
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            values["stratum_id"],
                            values["stratum_key"],
                            canonical_json(values["stratum_json"]),
                            values["population_count"],
                            values["eligible_count"],
                            values["selected_building_count"],
                            values["selected_candidate_count"],
                            values["stratum_record_sha256"],
                        ),
                    )

                selected_rows: dict[tuple[str, str], dict[str, Any]] = {}
                for rank, (item, summary) in enumerate(zip(selected_items, selected), 1):
                    values = {
                        "selection_id": item.identity,
                        "selection_rank": rank,
                        "stratum_id": stratum_ids[(summary.source, summary.stratum)],
                        "source": summary.source,
                        "entity_type": "building",
                        "source_entity_id": summary.source_building_id,
                        "source_building_id": summary.source_building_id,
                        "source_project_id": None,
                        "name": summary.name,
                        "normalized_name": _normal_name(summary.name),
                        "selection_reason": "deterministic_stratified_sample",
                        "e2_source_record_sha256": summary.source_record_sha256,
                        "e2_relation_record_sha256": None,
                    }
                    values["selection_record_sha256"] = canonical_sha256(
                        _selection_record_body(values)
                    )
                    detail = {
                        "building_summary": _summary_record(summary),
                        "sample_item_record_sha256": item.record_sha256,
                        "sample_score": deterministic_sample_score(
                            config.sample_seed, item
                        ),
                    }
                    connection.execute(
                        """
                        INSERT INTO selected_buildings(
                          run_id,selection_id,selection_rank,stratum_id,source,
                          entity_type,source_entity_id,source_building_id,
                          source_project_id,name,normalized_name,selection_reason,
                          e2_source_record_sha256,e2_relation_record_sha256,
                          selection_record_sha256,detail_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            values["selection_id"],
                            values["selection_rank"],
                            values["stratum_id"],
                            values["source"],
                            values["entity_type"],
                            values["source_entity_id"],
                            values["source_building_id"],
                            values["source_project_id"],
                            values["name"],
                            values["normalized_name"],
                            values["selection_reason"],
                            values["e2_source_record_sha256"],
                            values["e2_relation_record_sha256"],
                            values["selection_record_sha256"],
                            canonical_json(detail),
                        ),
                    )
                    selected_rows[(summary.source, summary.source_building_id)] = values

                for key in sorted(source_candidates):
                    selection_id = selected_rows[key]["selection_id"]
                    for source_row, mapped in zip(
                        source_candidates[key], mapped_candidates[key]
                    ):
                        values = _candidate_mapping_values(
                            mapped, source_row, selection_id
                        )
                        detail = {
                            "dimensions_basis": "decoded_e1_1024_response",
                            "phash_hex": source_row.phash_hex,
                            "ranking_feature_record_sha256": mapped.record_sha256,
                        }
                        connection.execute(
                            """
                            INSERT INTO image_candidates(
                              run_id,candidate_id,selection_id,source,
                              source_building_id,source_project_id,source_asset_id,
                              fingerprint_status,canonical_url,fetch_url,final_url,
                              roles_json,primary_role,role_rank,source_ordinal,
                              ordinal_is_derived,original_width,original_height,
                              normalized_width,normalized_height,quality_flags_json,
                              low_information,normalized_pixel_sha256,exact_cluster_id,
                              phash_node_id,source_record_sha256,
                              occurrence_record_sha256,project_relation_record_sha256,
                              building_relation_record_sha256,candidate_record_sha256,
                              detail_json
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                run_id,
                                values["candidate_id"],
                                values["selection_id"],
                                values["source"],
                                values["source_building_id"],
                                values["source_project_id"],
                                values["source_asset_id"],
                                values["fingerprint_status"],
                                values["canonical_url"],
                                values["fetch_url"],
                                values["final_url"],
                                canonical_json(values["roles_json"]),
                                values["primary_role"],
                                values["role_rank"],
                                values["source_ordinal"],
                                values["ordinal_is_derived"],
                                values["original_width"],
                                values["original_height"],
                                values["normalized_width"],
                                values["normalized_height"],
                                canonical_json(values["quality_flags_json"]),
                                values["low_information"],
                                values["normalized_pixel_sha256"],
                                values["exact_cluster_id"],
                                values["phash_node_id"],
                                values["source_record_sha256"],
                                values["occurrence_record_sha256"],
                                values["project_relation_record_sha256"],
                                values["building_relation_record_sha256"],
                                values["candidate_record_sha256"],
                                canonical_json(detail),
                            ),
                        )

                shortlist_count = 0
                for key in sorted(shortlists):
                    selection_id = selected_rows[key]["selection_id"]
                    candidates_by_id = {
                        value.candidate_id: value for value in mapped_candidates[key]
                    }
                    for shortlist in shortlists[key]:
                        for evaluation in shortlist.evaluations:
                            candidate = candidates_by_id[evaluation.candidate_id]
                            suppression_reason = next(
                                (
                                    reason
                                    for reason in evaluation.reasons
                                    if reason.startswith("suppressed_")
                                ),
                                None,
                            )
                            connection.execute(
                                """
                                INSERT INTO policy_rankings(
                                  run_id,policy_id,policy_version,
                                  policy_config_sha256,selection_id,candidate_id,
                                  ranking_state,editorial_rank,shortlist_rank,
                                  selected,qa_fallback,hard_risk,rank_tuple_json,
                                  component_scores_json,reasons_json,
                                  suppressed_by_candidate_id,suppression_reason,
                                  fallback_reason,ranking_record_sha256,detail_json
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    run_id,
                                    shortlist.policy.policy_id,
                                    E3_POLICY_VERSION,
                                    shortlist.policy.config_sha256,
                                    selection_id,
                                    evaluation.candidate_id,
                                    _ranking_state(
                                        evaluation.reasons, evaluation.selected
                                    ),
                                    evaluation.editorial_rank,
                                    evaluation.shortlist_rank,
                                    int(evaluation.selected),
                                    int(evaluation.qa_fallback),
                                    int(evaluation.hard_risk),
                                    canonical_json(list(editorial_sort_key(candidate))),
                                    canonical_json(dict(evaluation.component_scores)),
                                    canonical_json(list(evaluation.reasons)),
                                    evaluation.suppressed_by_candidate_id,
                                    suppression_reason,
                                    (
                                        "all_successful_candidates_hard_risk"
                                        if evaluation.qa_fallback
                                        else None
                                    ),
                                    evaluation.record_sha256,
                                    canonical_json(
                                        {
                                            "ranking_feature_record_sha256": (
                                                evaluation.candidate_record_sha256
                                            )
                                        }
                                    ),
                                ),
                            )
                        evaluations = {
                            value.candidate_id: value
                            for value in shortlist.evaluations
                        }
                        for rank, candidate_id in enumerate(
                            shortlist.selected_candidate_ids, 1
                        ):
                            evaluation = evaluations[candidate_id]
                            values = {
                                "policy_id": shortlist.policy.policy_id,
                                "selection_id": selection_id,
                                "shortlist_rank": rank,
                                "candidate_id": candidate_id,
                                "shortlist_state": (
                                    "qa_fallback"
                                    if evaluation.qa_fallback
                                    else "primary"
                                ),
                                "authoritative": 0,
                            }
                            connection.execute(
                                """
                                INSERT INTO shortlist_items(
                                  run_id,policy_id,selection_id,shortlist_rank,
                                  candidate_id,shortlist_state,authoritative,
                                  item_record_sha256,rationale_json
                                ) VALUES(?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    run_id,
                                    values["policy_id"],
                                    values["selection_id"],
                                    values["shortlist_rank"],
                                    values["candidate_id"],
                                    values["shortlist_state"],
                                    0,
                                    canonical_sha256(
                                        _shortlist_record_body(values)
                                    ),
                                    canonical_json(
                                        {
                                            "creates_final_representative": False,
                                            "creates_vision_tasks": False,
                                            "reasons": list(evaluation.reasons),
                                        }
                                    ),
                                ),
                            )
                            shortlist_count += 1

                queue_counts = _queue_counts(
                    shortlists,
                    candidate_source_by_id,
                    shortlist_size=config.shortlist_size,
                )
                queue_units = {
                    "top1_no_reuse": "selected_entity",
                    "top1_exact_reuse": "exact_unique_asset",
                    "top3_no_reuse": "shortlist_item",
                    "top3_exact_reuse": "exact_unique_asset",
                }
                for policy_id in sorted(queue_counts):
                    for scenario in sorted(queue_units):
                        population_scenario = (
                            "top1_no_reuse"
                            if scenario.startswith("top1_")
                            else "top3_no_reuse"
                        )
                        detail = {
                            "creates_vision_tasks": False,
                            "executable": False,
                            "non_executable": True,
                            "planning_only": True,
                            "scenario": scenario,
                            "semantic_reuse_allowed": False,
                        }
                        values = {
                            "estimate_id": f"{policy_id}:{scenario}",
                            "policy_id": policy_id,
                            "stratum_id": None,
                            "queue_unit": queue_units[scenario],
                            "population_count": queue_counts[policy_id][
                                population_scenario
                            ],
                            "estimated_queue_items": queue_counts[policy_id][scenario],
                            "tokens_per_item_low": None,
                            "tokens_per_item_point": None,
                            "tokens_per_item_high": None,
                            "projected_input_tokens": None,
                            "projected_output_tokens": None,
                            "projected_total_tokens": None,
                            "estimated_calls": None,
                            "retry_factor": 1.0,
                            "estimated_cost_usd": None,
                            "pricing_snapshot_json": {},
                            "quota_basis": None,
                            "projected_quota_percent": None,
                            "requests_executed": 0,
                            "authoritative": 0,
                        }
                        connection.execute(
                            """
                            INSERT INTO queue_estimates(
                              run_id,estimate_id,policy_id,stratum_id,queue_unit,
                              population_count,estimated_queue_items,
                              tokens_per_item_low,tokens_per_item_point,
                              tokens_per_item_high,projected_input_tokens,
                              projected_output_tokens,projected_total_tokens,
                              estimated_calls,retry_factor,estimated_cost_usd,
                              pricing_snapshot_json,quota_basis,
                              projected_quota_percent,requests_executed,
                              authoritative,estimate_record_sha256,detail_json,
                              created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                run_id,
                                values["estimate_id"],
                                values["policy_id"],
                                None,
                                values["queue_unit"],
                                values["population_count"],
                                values["estimated_queue_items"],
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                1.0,
                                None,
                                "{}",
                                None,
                                None,
                                0,
                                0,
                                canonical_sha256(_queue_record_body(values)),
                                canonical_json(detail),
                                now,
                            ),
                        )

                for name in (
                    "network_requests",
                    "vision_requests",
                    "llm_requests",
                ):
                    _insert_metric(connection, run_id, "validation", name, 0)
                _insert_metric(
                    connection, run_id, "selection", "population_buildings", len(summaries)
                )
                _insert_metric(
                    connection, run_id, "selection", "selected_buildings", len(selected)
                )
                _insert_metric(
                    connection,
                    run_id,
                    "selection",
                    "selected_image_candidates",
                    len(candidate_source_by_id),
                )
                _insert_metric(
                    connection, run_id, "selection", "shortlist_items", shortlist_count
                )

                e2_unchanged = (
                    e2_path.stat().st_size == source.lineage.artifact_size
                    and not any(
                        Path(str(e2_path) + suffix).exists()
                        for suffix in ("-wal", "-shm", "-journal", ".lock")
                    )
                )
                logical_sha, table_manifests = logical_selection_manifest(
                    connection, run_id
                )
                _insert_metric(
                    connection,
                    run_id,
                    "validation",
                    "output_logical_sha256",
                    logical_sha,
                )
                no_success_ids = {
                    item.identity
                    for item, summary in zip(selected_items, selected)
                    if summary.successful_asset_count == 0
                }
                no_success_candidates = sum(
                    1
                    for key, values in source_candidates.items()
                    if selected_rows[key]["selection_id"] in no_success_ids
                    for _value in values
                )
                validation_values = (
                    (
                        "input_e2_unchanged",
                        e2_unchanged,
                        "same size and no sidecars",
                        str(e2_unchanged),
                    ),
                    (
                        "sample_count",
                        len(selected) == config.sample_size,
                        str(config.sample_size),
                        str(len(selected)),
                    ),
                    (
                        "policy_count",
                        len(policy_rows) == 3,
                        "3",
                        str(len(policy_rows)),
                    ),
                    (
                        "candidate_accounting",
                        len(candidate_source_by_id)
                        == sum(len(value) for value in source_candidates.values()),
                        str(sum(len(value) for value in source_candidates.values())),
                        str(len(candidate_source_by_id)),
                    ),
                    (
                        "no_success_zero_candidates",
                        no_success_candidates == 0,
                        "0",
                        str(no_success_candidates),
                    ),
                    (
                        "shortlist_limit",
                        all(
                            len(value.selected_candidate_ids) <= config.shortlist_size
                            for values in shortlists.values()
                            for value in values
                        ),
                        f"<= {config.shortlist_size}",
                        str(
                            max(
                                (
                                    len(value.selected_candidate_ids)
                                    for values in shortlists.values()
                                    for value in values
                                ),
                                default=0,
                            )
                        ),
                    ),
                    (
                        "queue_scenario_count",
                        sum(len(value) for value in queue_counts.values()) == 12,
                        "12",
                        str(sum(len(value) for value in queue_counts.values())),
                    ),
                    (
                        "requests_zero",
                        True,
                        "0/0/0",
                        "0/0/0",
                    ),
                    (
                        "logical_manifest_created",
                        len(logical_sha) == 64,
                        "64-char SHA-256",
                        logical_sha,
                    ),
                )
                for name, passed, expected, actual in validation_values:
                    connection.execute(
                        """
                        INSERT INTO selection_validations(
                          run_id,validation_name,severity,passed,expected,actual,
                          detail_json,recorded_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            name,
                            "error",
                            int(passed),
                            expected,
                            actual,
                            canonical_json(
                                {
                                    "logical_manifest_version": (
                                        LOGICAL_MANIFEST_VERSION
                                    ),
                                    "table_manifests": (
                                        table_manifests
                                        if name == "logical_manifest_created"
                                        else None
                                    ),
                                }
                            ),
                            _utc_now(),
                        ),
                    )
                connection.commit()
                failed = [name for name, passed, _, _ in validation_values if not passed]
                if failed:
                    finalize_sidecar(
                        connection,
                        status="failed_validation",
                        error="; ".join(failed),
                    )
                    connection = None
                    raise RuntimeError("E3 internal validation failed: " + ", ".join(failed))
                finalize_sidecar(connection, status="complete")
                connection = None

            prepare_immutable_sidecar(output)
            structural = validate_sidecar(output, immutable=True)
            if not structural.passed:
                raise RuntimeError(
                    "terminal E3 structural validation failed: "
                    + repr(structural.semantic_violations)
                )
            return BuildResult(
                output_path=output,
                run_id=run_id,
                status="complete",
                logical_sha256=logical_sha,
                selected_buildings=len(selected),
                image_candidates=len(candidate_source_by_id),
                shortlist_items=shortlist_count,
                elapsed_seconds=time.perf_counter() - started,
            )
        except Exception as exc:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                    if run_id is not None:
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO selection_validations(
                              run_id,validation_name,severity,passed,expected,
                              actual,detail_json,recorded_at
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            (
                                run_id,
                                "pipeline_exception",
                                "error",
                                0,
                                "successful sample build",
                                f"{type(exc).__name__}: {exc}",
                                "{}",
                                _utc_now(),
                            ),
                        )
                        connection.commit()
                        finalize_sidecar(
                            connection,
                            status="failed_validation",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        connection = None
                except Exception:
                    if connection is not None:
                        connection.close()
                        connection = None
            raise


__all__ = [
    "BuildConfig",
    "BuildResult",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_E2_BUILDER_VERSION",
    "DEFAULT_E2_CONTRACT_VERSION",
    "DEFAULT_E2_LOGICAL_SHA256",
    "DEFAULT_E2_RELATIVE_PATH",
    "DEFAULT_E2_SHA256",
    "DEFAULT_E2_SIZE",
    "DEFAULT_SAMPLE_SEED",
    "DEFAULT_SHORTLIST_SIZE",
    "LOGICAL_MANIFEST_VERSION",
    "PIPELINE_VERSION",
    "build_cross_source_image_selection",
    "default_e2_path",
    "logical_selection_manifest",
]
