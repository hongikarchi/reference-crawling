from __future__ import annotations

import ast
import hashlib
import inspect
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from canonical.cross_source_image_selection import (
    E3_POLICY_VERSION,
    E3_SELECTION_VERSION,
    Candidate,
    DirectPHashEdge,
    SamplingItem,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    deterministic_stratified_sample,
    editorial_sort_key,
    ordered_sample_manifest_sha256,
    policy_definitions,
)
from canonical.cross_source_image_selection_sidecar import (
    finalize_sidecar,
    initialize_sidecar,
    recover_sidecar,
)
from canonical.cross_source_image_selection_pipeline import (
    BuildConfig,
    build_cross_source_image_selection,
)
from canonical.cross_source_image_selection_sources import open_e2_selection_sources
from canonical.cross_source_image_selection_validator import (
    EXPECTED_INDEX_NAMES,
    LOGICAL_MANIFEST_VERSION,
    logical_selection_manifest,
    open_immutable,
    validate_e3_artifact,
)
import canonical.cross_source_image_selection_validator as validator_module
import canonical.cross_source_image_selection_sources as source_module
from tools.validate_cross_source_image_selection_e3 import main as validator_cli_main
from tests.test_cross_source_image_selection_sources import (
    _create_e2_fixture,
    _spec as e2_fixture_spec,
)


RUN_ID = "e3-validator-fixture"
STAMP = "2026-08-11T00:00:00Z"


def _insert_run(
    connection: sqlite3.Connection,
    *,
    selection_mode: str = "sample",
    sample_size: int | None = 1,
    sample_seed: str | None = "fixture-seed",
) -> None:
    connection.execute(
        """
        INSERT INTO selection_runs(
          run_id,contract_version,builder_version,e2_artifact_path,
          e2_size_bytes,e2_byte_sha256,e2_logical_sha256,policy_set_sha256,
          selection_mode,sample_size,sample_seed,shortlist_size,
          ordered_selection_manifest_sha256,config_json,
          network_requests,vision_requests,llm_requests,authoritative,
          artifact_scope,status,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            E3_SELECTION_VERSION,
            "fixture-builder-v1",
            "C:/immutable/e2.db",
            1,
            "1" * 64,
            "2" * 64,
            "3" * 64,
            selection_mode,
            sample_size,
            sample_seed,
            3,
            "4" * 64,
            '{"creates_final_representative":false,"creates_vision_tasks":false}',
            0,
            0,
            0,
            0,
            "candidate_only",
            "building",
            STAMP,
        ),
    )


def _summary_sha(summary) -> str:
    return canonical_sha256(
        {
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
    )


def _sample_item(summary) -> SamplingItem:
    return SamplingItem(
        identity=f"{summary.source}:building:{summary.source_building_id}",
        source=summary.source,
        stratum=summary.stratum,
        input_record_sha256=_summary_sha(summary),
    )


def _primary_role(roles: tuple[str, ...]) -> str:
    if "cover" in roles:
        return "cover"
    if "gallery" in roles:
        return "gallery"
    return min(roles)


def _mapped_candidate(source) -> Candidate:
    return Candidate(
        source=source.source,
        source_building_id=source.source_building_id,
        source_asset_id=source.source_asset_id,
        fingerprint_status="success",
        role=_primary_role(source.roles),
        ordinal=source.lowest_project_ordinal,
        original_width=source.original_width,
        original_height=source.original_height,
        quality_flags=source.quality_flags,
        source_record_sha256=source.source_asset_record_sha256,
        exact_cluster_id=source.exact_cluster_id,
        phash_node_id=source.phash_node_id,
        canonical_url=source.canonical_url,
    )


def _stratum_id(source: str, stratum: str) -> str:
    return "e3str_" + canonical_sha256(
        {"source": source, "stratum": stratum, "version": E3_SELECTION_VERSION}
    )


def _policy_row(policy) -> dict[str, object]:
    definition = policy.as_config()
    row = {
        "policy_id": policy.policy_id,
        "policy_version": E3_POLICY_VERSION,
        "policy_name": policy.policy_id,
        "description": policy.description,
        "shortlist_size": policy.shortlist_size,
        "enabled": True,
        "definition": definition,
        "policy_config_sha256": policy.config_sha256,
    }
    row["policy_record_sha256"] = canonical_sha256(row)
    return row


def _candidate_values(source, candidate: Candidate, selection_id: str) -> dict[str, object]:
    role_rank = 0 if candidate.role == "cover" else 1 if candidate.role == "gallery" else 2
    values: dict[str, object] = {
        "building_relation_record_sha256": source.building_relation_record_sha256,
        "candidate_id": candidate.candidate_id,
        "canonical_url": source.canonical_url,
        "exact_cluster_id": source.exact_cluster_id,
        "fetch_url": source.fetch_url,
        "final_url": source.final_url,
        "fingerprint_status": "success",
        "low_information": int("low_information" in source.quality_flags),
        "normalized_height": source.normalized_height,
        "normalized_pixel_sha256": source.normalized_pixel_sha256,
        "normalized_width": source.normalized_width,
        "occurrence_record_sha256": None,
        "ordinal_is_derived": int(source.lowest_project_ordinal is not None),
        "original_height": source.original_height,
        "original_width": source.original_width,
        "phash_node_id": source.phash_node_id,
        "primary_role": candidate.role,
        "project_relation_record_sha256": None,
        "quality_flags_json": list(source.quality_flags),
        "role_rank": role_rank,
        "roles_json": list(source.roles),
        "selection_id": selection_id,
        "source": source.source,
        "source_asset_id": source.source_asset_id,
        "source_building_id": source.source_building_id,
        "source_ordinal": source.lowest_project_ordinal,
        "source_project_id": None,
        "source_record_sha256": source.source_asset_record_sha256,
    }
    values["candidate_record_sha256"] = canonical_sha256(values)
    return values


def _drop_update_guard_and_mutate(
    path: Path,
    table: str,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> None:
    connection = sqlite3.connect(path)
    trigger = f"{table}_terminal_update_guard"
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()[0]
    connection.execute(f'DROP TRIGGER "{trigger}"')
    connection.execute(sql, parameters)
    connection.execute(str(trigger_sql))
    connection.commit()
    connection.close()


def _copy_fixture(source: Path, target: Path) -> Path:
    shutil.copy2(source, target)
    return target


def _build_valid_fixture(
    root: Path,
    *,
    selection_mode: str = "sample",
) -> tuple[Path, Path]:
    assert selection_mode in {"sample", "full"}
    root.mkdir()
    e2_path = root / "e2.db"
    _create_e2_fixture(e2_path)
    # Give one ordinary building the A--B--C direct-edge chain from the source
    # fixture.  A--C remains distance 10 and must not be traversed through B.
    e2_write = sqlite3.connect(e2_path)
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    e2_write.executemany(
        "INSERT INTO project_assets VALUES (?,?,?,?,?)",
        [
            ("e2-selection-source-fixture", "divisare", "p4", "gallery", 1),
            ("e2-selection-source-fixture", "divisare", "p4", "cross", 2),
            ("e2-selection-source-fixture", "divisare", "p4", "risk-dim", 3),
            ("e2-selection-source-fixture", "divisare", "p4", "risk-low", 4),
        ],
    )
    e2_write.executemany(
        "INSERT INTO building_assets VALUES (?,?,?,?,?,?)",
        [
            (
                "e2-selection-source-fixture",
                "divisare",
                "b4",
                "gallery",
                '["gallery"]',
                digest("fixture:b4:gallery"),
            ),
            (
                "e2-selection-source-fixture",
                "divisare",
                "b4",
                "cross",
                '["gallery"]',
                digest("fixture:b4:cross"),
            ),
            (
                "e2-selection-source-fixture",
                "divisare",
                "b4",
                "risk-dim",
                '["gallery"]',
                digest("fixture:b4:risk-dim"),
            ),
            (
                "e2-selection-source-fixture",
                "divisare",
                "b4",
                "risk-low",
                '["gallery"]',
                digest("fixture:b4:risk-low"),
            ),
        ],
    )
    for source_name, asset_id in e2_write.execute(
        "SELECT source,source_asset_id FROM assets WHERE fingerprint_status='success'"
    ).fetchall():
        e2_write.execute(
            """
            UPDATE assets SET normalized_pixel_sha256=?,phash_hex=?
            WHERE source=? AND source_asset_id=?
            """,
            (
                digest(f"pixel:{source_name}:{asset_id}"),
                digest(f"phash:{source_name}:{asset_id}"),
                source_name,
                asset_id,
            ),
        )
    e2_write.commit()
    e2_write.close()
    e2_spec = e2_fixture_spec(e2_path)

    with open_e2_selection_sources(e2_spec) as source:
        summaries = tuple(source.iter_building_summaries())
        items = tuple(_sample_item(summary) for summary in summaries)
        selected_items = (
            deterministic_stratified_sample(
                items, sample_size=len(items), seed="validator-seed"
            )
            if selection_mode == "sample"
            else items
        )
        summaries_by_id = {
            item.identity: summary for item, summary in zip(items, summaries)
        }
        selected = tuple(summaries_by_id[item.identity] for item in selected_items)
        candidate_groups = {
            (summary.source, summary.source_building_id): tuple(
                source.iter_candidates(summary.source, summary.source_building_id)
            )
            for summary in selected
        }
        mapped_groups = {
            key: tuple(_mapped_candidate(value) for value in values)
            for key, values in candidate_groups.items()
        }
        direct_by_building: dict[
            tuple[str, str], dict[tuple[str, str], DirectPHashEdge]
        ] = defaultdict(dict)
        direct_evidence_by_building: dict[
            tuple[str, str], dict[tuple[str, str], dict[str, object]]
        ] = defaultdict(dict)
        same_building_edges = tuple(source.iter_same_building_direct_phash_edges())
        for edge in same_building_edges:
            key = (edge.source, edge.source_building_id)
            by_asset = {
                row.source_asset_id: candidate
                for row, candidate in zip(candidate_groups[key], mapped_groups[key])
            }
            left = by_asset[edge.left_source_asset_id]
            right = by_asset[edge.right_source_asset_id]
            assert left.phash_node_id == edge.left_node_id
            assert right.phash_node_id == edge.right_node_id
            mapped_edge = DirectPHashEdge(
                left.candidate_id,
                right.candidate_id,
                edge.hamming_distance,
            )
            direct_by_building[key][mapped_edge.pair] = mapped_edge
            direct_evidence_by_building[key][mapped_edge.pair] = {
                "distance": edge.hamming_distance,
                "edge_id": edge.edge_id,
                "edge_record_sha256": edge.edge_record_sha256,
                "left_candidate_id": mapped_edge.pair[0],
                "right_candidate_id": mapped_edge.pair[1],
            }

        policies = policy_definitions(3)
        policy_rows = [_policy_row(policy) for policy in policies]
        policy_set_sha = canonical_sha256(
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
        artifact = root / "e3.db"
        connection = initialize_sidecar(artifact)
        run_id = RUN_ID
        ordered_manifest = ordered_sample_manifest_sha256(selected_items)
        config = {
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "network_enabled": False,
            "phash_transitive_closure_allowed": False,
            "semantic_reuse_allowed": False,
        }
        connection.execute(
            """
            INSERT INTO selection_runs(
              run_id,contract_version,builder_version,e2_artifact_path,
              e2_size_bytes,e2_byte_sha256,e2_logical_sha256,policy_set_sha256,
              selection_mode,sample_size,sample_seed,shortlist_size,
              ordered_selection_manifest_sha256,config_json,
              network_requests,vision_requests,llm_requests,authoritative,
              artifact_scope,status,started_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                E3_SELECTION_VERSION,
                "fixture-builder-v1",
                str(e2_path.resolve()),
                e2_spec.expected_size,
                e2_spec.expected_sha256,
                e2_spec.expected_logical_sha256,
                policy_set_sha,
                selection_mode,
                len(items) if selection_mode == "sample" else None,
                "validator-seed" if selection_mode == "sample" else None,
                3,
                ordered_manifest,
                canonical_json(config),
                0,
                0,
                0,
                0,
                "candidate_only",
                "building",
                STAMP,
            ),
        )
        lineage = source.lineage
        input_detail = {
            "e2_builder_version": lineage.builder_version,
            "e2_contract_version": lineage.contract_version,
            "e2_logical_sha256": lineage.stored_logical_sha256,
            "e2_ordered_selection_manifest_sha256": lineage.ordered_selection_manifest_sha256,
            "e2_run_id": lineage.run_id,
            "e2_selection_mode": lineage.selection_mode,
        }
        e2_schema_manifest = canonical_sha256(
            [
                {
                    "name": row[1],
                    "sql": row[3],
                    "table": row[2],
                    "type": row[0],
                }
                for row in source.connection.execute(
                    """
                    SELECT type,name,tbl_name,sql FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    ORDER BY type,name
                    """
                )
            ]
        )
        connection.execute(
            """
            INSERT INTO selection_inputs(
              run_id,input_name,input_role,file_path,size_bytes,sha256_before,
              sha256_after,logical_sha256,application_id,user_version,
              schema_manifest_sha256,recorded_at,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                "e2_evidence",
                "e2_evidence",
                str(e2_path.resolve()),
                e2_spec.expected_size,
                e2_spec.expected_sha256,
                e2_spec.expected_sha256,
                e2_spec.expected_logical_sha256,
                0,
                0,
                e2_schema_manifest,
                STAMP,
                canonical_json(input_detail),
            ),
        )
        for row in policy_rows:
            connection.execute(
                """
                INSERT INTO policy_definitions VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    row["policy_id"],
                    row["policy_version"],
                    row["policy_name"],
                    row["description"],
                    row["shortlist_size"],
                    int(bool(row["enabled"])),
                    canonical_json(row["definition"]),
                    row["policy_config_sha256"],
                    row["policy_record_sha256"],
                    STAMP,
                ),
            )

        population = Counter((value.source, value.stratum) for value in summaries)
        eligible = Counter(
            (value.source, value.stratum)
            for value in summaries
            if value.successful_asset_count > 0
        )
        selected_counts = Counter((value.source, value.stratum) for value in selected)
        candidate_counts = Counter()
        for summary in selected:
            candidate_counts[(summary.source, summary.stratum)] += len(
                candidate_groups[(summary.source, summary.source_building_id)]
            )
        for cell in sorted(population):
            source_name, stratum = cell
            payload = {"source": source_name, "stratum": stratum}
            stratum_id = _stratum_id(*cell)
            body = {
                "eligible_count": eligible[cell],
                "population_count": population[cell],
                "selected_building_count": selected_counts[cell],
                "selected_candidate_count": candidate_counts[cell],
                "stratum": payload,
                "stratum_id": stratum_id,
                "stratum_key": f"{source_name}:{stratum}",
            }
            connection.execute(
                "INSERT INTO population_strata VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    stratum_id,
                    body["stratum_key"],
                    canonical_json(payload),
                    body["population_count"],
                    body["eligible_count"],
                    body["selected_building_count"],
                    body["selected_candidate_count"],
                    canonical_sha256(body),
                ),
            )

        selected_rows: dict[tuple[str, str], str] = {}
        for rank, (item, summary) in enumerate(zip(selected_items, selected), 1):
            selection_id = item.identity
            selected_rows[(summary.source, summary.source_building_id)] = selection_id
            body = {
                "e2_relation_record_sha256": None,
                "e2_source_record_sha256": summary.source_record_sha256,
                "entity_type": "building",
                "name": summary.name,
                "normalized_name": " ".join(summary.name.casefold().split()),
                "selection_id": selection_id,
                "selection_rank": rank,
                "selection_reason": (
                    "deterministic_stratified_sample"
                    if selection_mode == "sample"
                    else "full_population"
                ),
                "source": summary.source,
                "source_building_id": summary.source_building_id,
                "source_entity_id": summary.source_building_id,
                "source_project_id": None,
                "stratum_id": _stratum_id(summary.source, summary.stratum),
            }
            connection.execute(
                "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    body["selection_id"],
                    body["selection_rank"],
                    body["stratum_id"],
                    body["source"],
                    body["entity_type"],
                    body["source_entity_id"],
                    body["source_building_id"],
                    body["source_project_id"],
                    body["name"],
                    body["normalized_name"],
                    body["selection_reason"],
                    body["e2_source_record_sha256"],
                    body["e2_relation_record_sha256"],
                    canonical_sha256(body),
                    "{}",
                ),
            )

        for key in sorted(candidate_groups):
            selection_id = selected_rows[key]
            for source_row, candidate in zip(candidate_groups[key], mapped_groups[key]):
                values = _candidate_values(source_row, candidate, selection_id)
                connection.execute(
                    """
                    INSERT INTO image_candidates VALUES(
                      ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
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
                        canonical_json(
                            {
                                "phash_hex": source_row.phash_hex,
                                "ranking_feature_record_sha256": candidate.record_sha256,
                            }
                        ),
                    ),
                )

        for key in sorted(mapped_groups):
            candidates = mapped_groups[key]
            if not candidates:
                continue
            selection_id = selected_rows[key]
            shortlists = compare_standard_policies(
                candidates,
                shortlist_size=3,
                direct_phash_edges=tuple(direct_by_building[key].values()),
            )
            by_candidate = {value.candidate_id: value for value in candidates}
            for shortlist in shortlists:
                for evaluation in shortlist.evaluations:
                    candidate = by_candidate[evaluation.candidate_id]
                    suppression = next(
                        (
                            reason
                            for reason in evaluation.reasons
                            if reason.startswith("suppressed_")
                        ),
                        None,
                    )
                    if evaluation.selected:
                        state = "shortlisted"
                    elif suppression:
                        state = "suppressed"
                    elif "excluded_quality_hard_risk" in evaluation.reasons:
                        state = "ineligible"
                    else:
                        state = "ranked"
                    edge_detail = None
                    if (
                        suppression == "suppressed_direct_phash_le8"
                        and evaluation.suppressed_by_candidate_id is not None
                    ):
                        pair = tuple(
                            sorted(
                                (
                                    evaluation.candidate_id,
                                    evaluation.suppressed_by_candidate_id,
                                )
                            )
                        )
                        edge_detail = direct_evidence_by_building[key][pair]
                    connection.execute(
                        """
                        INSERT INTO policy_rankings VALUES(
                          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                        )
                        """,
                        (
                            run_id,
                            shortlist.policy.policy_id,
                            E3_POLICY_VERSION,
                            shortlist.policy.config_sha256,
                            selection_id,
                            evaluation.candidate_id,
                            state,
                            evaluation.editorial_rank,
                            evaluation.shortlist_rank,
                            int(evaluation.selected),
                            int(evaluation.qa_fallback),
                            int(evaluation.hard_risk),
                            canonical_json(list(editorial_sort_key(candidate))),
                            canonical_json(dict(evaluation.component_scores)),
                            canonical_json(list(evaluation.reasons)),
                            evaluation.suppressed_by_candidate_id,
                            suppression,
                            (
                                "all_successful_candidates_hard_risk"
                                if evaluation.qa_fallback
                                else None
                            ),
                            evaluation.record_sha256,
                            canonical_json(
                                {
                                    "direct_phash_edge": edge_detail,
                                    "ranking_feature_record_sha256": (
                                        evaluation.candidate_record_sha256
                                    ),
                                }
                            ),
                        ),
                    )
                by_evaluation = {
                    value.candidate_id: value for value in shortlist.evaluations
                }
                for rank, candidate_id in enumerate(shortlist.selected_candidate_ids, 1):
                    evaluation = by_evaluation[candidate_id]
                    state = "qa_fallback" if evaluation.qa_fallback else "primary"
                    body = {
                        "authoritative": False,
                        "candidate_id": candidate_id,
                        "policy_id": shortlist.policy.policy_id,
                        "selection_id": selection_id,
                        "shortlist_rank": rank,
                        "shortlist_state": state,
                    }
                    connection.execute(
                        "INSERT INTO shortlist_items VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            body["policy_id"],
                            selection_id,
                            rank,
                            candidate_id,
                            state,
                            0,
                            canonical_sha256(body),
                            "{}",
                        ),
                    )

        scenarios = {
            "top1_no_reuse": "selected_entity",
            "top1_exact_reuse": "exact_unique_asset",
            "top3_no_reuse": "shortlist_item",
            "top3_exact_reuse": "exact_unique_asset",
        }
        for policy in policies:
            counts = connection.execute(
                """
                SELECT sum(si.shortlist_rank=1),
                       count(DISTINCT CASE WHEN si.shortlist_rank=1
                                      THEN ic.normalized_pixel_sha256 END),
                       sum(si.shortlist_rank<=3),
                       count(DISTINCT CASE WHEN si.shortlist_rank<=3
                                      THEN ic.normalized_pixel_sha256 END)
                FROM shortlist_items si JOIN image_candidates ic
                  ON ic.run_id=si.run_id AND ic.candidate_id=si.candidate_id
                WHERE si.run_id=? AND si.policy_id=?
                """,
                (run_id, policy.policy_id),
            ).fetchone()
            scenario_counts = dict(
                zip(scenarios, (int(counts[0] or 0), int(counts[1] or 0), int(counts[2] or 0), int(counts[3] or 0)))
            )
            for scenario, unit in scenarios.items():
                population_scenario = (
                    "top1_no_reuse" if scenario.startswith("top1_") else "top3_no_reuse"
                )
                detail = {
                    "creates_vision_tasks": False,
                    "executable": False,
                    "non_executable": True,
                    "planning_only": True,
                    "scenario": scenario,
                    "semantic_reuse_allowed": False,
                }
                estimate_id = f"{policy.policy_id}:{scenario}"
                body = {
                    "authoritative": False,
                    "estimated_calls": None,
                    "estimated_cost_usd": None,
                    "estimated_queue_items": scenario_counts[scenario],
                    "estimate_id": estimate_id,
                    "policy_id": policy.policy_id,
                    "population_count": scenario_counts[population_scenario],
                    "pricing_snapshot": {},
                    "projected_input_tokens": None,
                    "projected_output_tokens": None,
                    "projected_quota_percent": None,
                    "projected_total_tokens": None,
                    "queue_unit": unit,
                    "quota_basis": None,
                    "requests_executed": 0,
                    "retry_factor": 1.0,
                    "stratum_id": None,
                    "tokens_per_item_high": None,
                    "tokens_per_item_low": None,
                    "tokens_per_item_point": None,
                }
                connection.execute(
                    """
                    INSERT INTO queue_estimates VALUES(
                      ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        run_id,
                        estimate_id,
                        policy.policy_id,
                        None,
                        unit,
                        body["population_count"],
                        body["estimated_queue_items"],
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
                        canonical_sha256(body),
                        canonical_json(detail),
                        STAMP,
                    ),
                )

        if selection_mode == "full":
            counter_json = lambda values: {
                f"{source_name}:{stratum}": int(values[(source_name, stratum)])
                for source_name, stratum in sorted(values)
            }
            population_last_key = [
                summaries[-1].source,
                summaries[-1].source_building_id,
            ]
            candidate_keys = [
                key for key in sorted(candidate_groups) if candidate_groups[key]
            ]
            candidate_count = sum(
                len(candidate_groups[key]) for key in candidate_keys
            )
            checkpoint_values = {
                "inventory": (
                    {
                        "eligible_counts": counter_json(eligible),
                        "last_key": population_last_key,
                        "population_counts": counter_json(population),
                    },
                    len(summaries),
                ),
                "selection": (
                    {
                        "last_key": population_last_key,
                        "ordered_manifest_sha256": ordered_manifest,
                    },
                    len(selected),
                ),
                "candidates": (
                    {
                        "completed_buildings": len(candidate_keys),
                        "direct_edge_rows": len(same_building_edges),
                        "last_key": list(candidate_keys[-1]),
                        "ranking_rows": int(
                            connection.execute(
                                "SELECT count(*) FROM policy_rankings WHERE run_id=?",
                                (run_id,),
                            ).fetchone()[0]
                        ),
                        "shortlist_rows": int(
                            connection.execute(
                                "SELECT count(*) FROM shortlist_items WHERE run_id=?",
                                (run_id,),
                            ).fetchone()[0]
                        ),
                    },
                    candidate_count,
                ),
            }
            for phase, (cursor, completed_rows) in checkpoint_values.items():
                connection.execute(
                    "INSERT INTO build_checkpoints VALUES(?,?,?,?,?,?)",
                    (
                        run_id,
                        phase,
                        canonical_json(cursor),
                        completed_rows,
                        1,
                        STAMP,
                    ),
                )

        for name in ("network_requests", "vision_requests", "llm_requests"):
            connection.execute(
                "INSERT INTO selection_metrics VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "execution", name, "{}", 0, None, None, STAMP),
            )
        connection.execute(
            "INSERT INTO selection_validations VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "fixture", "error", 1, "0", "0", "{}", STAMP),
        )
        logical_sha, _ = logical_selection_manifest(connection, run_id)
        connection.execute(
            "INSERT INTO selection_metrics VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                "validation",
                "output_logical_sha256",
                "{}",
                None,
                None,
                logical_sha,
                STAMP,
            ),
        )
        connection.commit()
        finalize_sidecar(connection, status="complete", completed_at=STAMP)
    return artifact, e2_path


def _empty_schema(path: Path) -> None:
    connection = initialize_sidecar(path)
    connection.close()
    recover_sidecar(path, switch_to_delete=True)


def _check(report: object, name: str):
    return next(check for check in report.checks if check.name == name)


def test_frozen_schema_contract_accepts_initialized_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    _empty_schema(path)

    report = validate_e3_artifact(path)

    assert not report.passed
    assert _check(report, "exact_table_set").passed
    assert _check(report, "exact_table_columns").passed
    assert _check(report, "exact_index_set").passed
    assert _check(report, "exact_trigger_set").passed
    assert report.failed_check_names == ("exactly_one_complete_candidate_run",)


def test_valid_terminal_sample_passes_full_independent_validation(
    tmp_path: Path,
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "valid")

    report = validate_e3_artifact(artifact)

    assert report.passed, report.failed_check_names
    assert report.run_id == RUN_ID
    assert report.logical_sha256 is not None
    assert len(report.logical_sha256) == 64
    assert report.input_files[0].passed
    assert all(check.passed for check in report.checks)
    assert not Path(str(artifact) + "-wal").exists()
    assert not Path(str(artifact) + "-shm").exists()


def test_real_pipeline_output_passes_independent_validator_with_top3_cap(
    tmp_path: Path,
) -> None:
    _manual, e2_path = _build_valid_fixture(tmp_path / "pipeline-input")
    spec = e2_fixture_spec(e2_path)
    output = tmp_path / "pipeline-output.db"

    result = build_cross_source_image_selection(
        BuildConfig(
            e2_path=e2_path,
            output_path=output,
            expected_e2_size=spec.expected_size,
            expected_e2_sha256=spec.expected_sha256,
            expected_e2_logical_sha256=spec.expected_logical_sha256,
            expected_e2_contract_version=spec.expected_contract_version,
            expected_e2_builder_version=spec.expected_builder_version,
            sample_size=6,
            sample_seed="validator-seed",
            shortlist_size=5,
            batch_size=2,
        )
    )
    report = validate_e3_artifact(output)

    assert result.status == "complete"
    assert result.logical_sha256 == report.logical_sha256
    assert report.passed, report.failed_check_names
    connection = open_immutable(output)
    try:
        counts = connection.execute(
            """
            SELECT q.policy_id,q.estimated_queue_items,
                   (SELECT count(*) FROM shortlist_items si
                    WHERE si.run_id=q.run_id AND si.policy_id=q.policy_id
                      AND si.shortlist_rank<=3),
                   (SELECT count(*) FROM shortlist_items si
                    WHERE si.run_id=q.run_id AND si.policy_id=q.policy_id)
            FROM queue_estimates q
            WHERE json_extract(q.detail_json,'$.scenario')='top3_no_reuse'
            ORDER BY q.policy_id
            """
        ).fetchall()
    finally:
        connection.close()
    assert len(counts) == 3
    assert all(int(row[1]) == int(row[2]) for row in counts)
    p0 = next(row for row in counts if str(row[0]) == "p0_editorial_baseline")
    assert int(p0[3]) > int(p0[2])


def test_validator_hashes_e2_exactly_once_then_uses_final_stat(
    tmp_path: Path, monkeypatch
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "one-hash")
    original = source_module._sha256_file
    calls: list[Path] = []

    def counted(path: Path, *args, **kwargs):
        calls.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(source_module, "_sha256_file", counted)
    report = validate_e3_artifact(artifact)

    assert report.passed
    assert len(calls) == 1
    assert _check(report, "e2_unchanged_after_full_validation_read").passed


def test_no_success_is_selected_with_zero_candidate_rows_and_p2_is_chosen_star(
    tmp_path: Path,
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "boundaries")
    connection = open_immutable(artifact)
    try:
        no_success = "divisare:building:b0"
        assert connection.execute(
            "SELECT count(*) FROM selected_buildings WHERE selection_id=?",
            (no_success,),
        ).fetchone()[0] == 1
        for table in ("image_candidates", "policy_rankings", "shortlist_items"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE selection_id=?", (no_success,)
            ).fetchone()[0] == 0

        b4 = "divisare:building:b4"
        p2_rows = connection.execute(
            """
            SELECT ic.source_asset_id,pr.selected,pr.suppression_reason,
                   sup.source_asset_id
            FROM policy_rankings pr
            JOIN image_candidates ic
              ON ic.run_id=pr.run_id AND ic.candidate_id=pr.candidate_id
            LEFT JOIN image_candidates sup
              ON sup.run_id=pr.run_id
             AND sup.candidate_id=pr.suppressed_by_candidate_id
            WHERE pr.selection_id=?
              AND pr.policy_id='p2_quality_exact_direct_phash_shortlist'
            ORDER BY pr.editorial_rank
            """,
            (b4,),
        ).fetchall()
    finally:
        connection.close()
    by_asset = {str(row[0]): row for row in p2_rows}
    assert int(by_asset["ordinary"][1]) == 1
    assert int(by_asset["gallery"][1]) == 1
    assert int(by_asset["cross"][1]) == 0
    assert str(by_asset["cross"][2]) == "suppressed_direct_phash_le8"
    assert str(by_asset["cross"][3]) == "ordinary"


def test_independent_validator_detects_tamper_at_every_semantic_layer(
    tmp_path: Path,
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "tamper-base")
    cases = (
        (
            "selection_inputs",
            "UPDATE selection_inputs SET detail_json='{}' WHERE input_role='e2_evidence'",
            "e2_immutable_contract_and_stored_logical_sha",
        ),
        (
            "policy_definitions",
            "UPDATE policy_definitions SET description=description||'-tampered' WHERE policy_id=(SELECT min(policy_id) FROM policy_definitions)",
            "policy_configs_and_record_hashes",
        ),
        (
            "population_strata",
            "UPDATE population_strata SET population_count=population_count+1 WHERE stratum_id=(SELECT min(stratum_id) FROM population_strata)",
            "population_eligibility_sample_quotas",
        ),
        (
            "selected_buildings",
            "UPDATE selected_buildings SET name=name||'-tampered' WHERE selection_rank=1",
            "selected_building_sample_source_and_hashes",
        ),
        (
            "image_candidates",
            "UPDATE image_candidates SET canonical_url=canonical_url||'?tampered=1' WHERE candidate_id=(SELECT min(candidate_id) FROM image_candidates)",
            "candidate_e2_fields_relations_and_record_hashes",
        ),
        (
            "policy_rankings",
            "UPDATE policy_rankings SET reasons_json='[\"tampered\"]' WHERE rowid=(SELECT min(rowid) FROM policy_rankings)",
            "p0_p1_p2_rankings_and_nontransitive_chosen_star",
        ),
        (
            "shortlist_items",
            "UPDATE shortlist_items SET shortlist_state=CASE shortlist_state WHEN 'primary' THEN 'qa_fallback' ELSE 'primary' END WHERE rowid=(SELECT min(rowid) FROM shortlist_items)",
            "p0_p1_p2_rankings_and_nontransitive_chosen_star",
        ),
        (
            "queue_estimates",
            "UPDATE queue_estimates SET estimated_queue_items=estimated_queue_items+1 WHERE estimate_id=(SELECT min(estimate_id) FROM queue_estimates)",
            "queue_estimate_accounting_and_non_authoritative_flags",
        ),
        (
            "selection_metrics",
            "UPDATE selection_metrics SET value_integer=1 WHERE metric_name='network_requests'",
            "request_metrics_exact_zero",
        ),
        (
            "selection_metrics",
            "UPDATE selection_metrics SET value_text='0000000000000000000000000000000000000000000000000000000000000000' WHERE metric_name='output_logical_sha256'",
            "logical_manifest_matches_stored",
        ),
    )
    for index, (table, sql, expected_check) in enumerate(cases):
        tampered = _copy_fixture(artifact, tmp_path / f"tampered-{index}.db")
        _drop_update_guard_and_mutate(tampered, table, sql)

        report = validate_e3_artifact(tampered)

        assert not report.passed
        assert not _check(report, expected_check).passed, (index, expected_check)


def test_output_sidecar_presence_is_reported_without_writing(
    tmp_path: Path,
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "output-sidecar")
    fake_wal = Path(str(artifact) + "-wal")
    fake_wal.write_bytes(b"not-a-real-wal")
    try:
        report = validate_e3_artifact(artifact)
    finally:
        fake_wal.unlink()

    assert not report.passed
    assert not _check(report, "output_sqlite_sidecars_absent_at_open").passed


def test_schema_index_and_forbidden_table_tamper_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "tampered-schema.db"
    connection = initialize_sidecar(path)
    connection.execute(f"DROP INDEX {sorted(EXPECTED_INDEX_NAMES)[0]}")
    connection.execute("CREATE TABLE representative_images(id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()

    report = validate_e3_artifact(path)

    assert not _check(report, "exact_table_set").passed
    assert not _check(report, "exact_index_set").passed
    assert not _check(report, "forbidden_policy_tables_absent").passed


def test_logical_manifest_binds_header_not_only_table_rows(tmp_path: Path) -> None:
    manifests: list[str] = []
    for index, seed in enumerate(("seed-a", "seed-b")):
        path = tmp_path / f"logical-{index}.db"
        connection = initialize_sidecar(path)
        _insert_run(connection, sample_seed=seed)
        connection.commit()
        digest, tables = logical_selection_manifest(connection, RUN_ID)
        manifests.append(digest)
        assert set(tables)
        connection.close()

    assert manifests[0] != manifests[1]
    assert LOGICAL_MANIFEST_VERSION == "archibe-e3-selection-logical-manifest-v1"


def test_streaming_ordered_manifest_matches_frozen_core_contract() -> None:
    items = tuple(
        SamplingItem(
            identity=f"divisare:building:{index}",
            source="divisare",
            stratum="ordinary",
            input_record_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
        )
        for index in range(250)
    )
    streaming = validator_module._OrderedSelectionManifestHasher()
    for item in items:
        streaming.add(item)

    assert streaming.hexdigest() == ordered_sample_manifest_sha256(items)


def test_full_mode_passes_bounded_merge_stream_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact, _e2 = _build_valid_fixture(
        tmp_path / "full-valid",
        selection_mode="full",
    )
    calls = {
        "summaries": 0,
        "all_candidates": 0,
        "per_building": 0,
        "global_edges": 0,
        "same_building_edges": 0,
    }
    original_summaries = source_module.E2SelectionSources.iter_building_summaries
    original_all = source_module.E2SelectionSources.iter_all_candidates
    original_one = source_module.E2SelectionSources.iter_candidates
    original_same_building_edges = (
        source_module.E2SelectionSources.iter_same_building_direct_phash_edges
    )

    def counted_summaries(self, *args, **kwargs):
        calls["summaries"] += 1
        return original_summaries(self, *args, **kwargs)

    def counted_all(self, *args, **kwargs):
        calls["all_candidates"] += 1
        return original_all(self, *args, **kwargs)

    def forbidden_per_building(self, *args, **kwargs):
        calls["per_building"] += 1
        return original_one(self, *args, **kwargs)

    def forbidden_global_edges(self, *args, **kwargs):
        calls["global_edges"] += 1
        raise AssertionError("full validator must not materialize global direct edges")

    def counted_same_building_edges(self, *args, **kwargs):
        calls["same_building_edges"] += 1
        assert not args and not kwargs
        return original_same_building_edges(self)

    monkeypatch.setattr(
        source_module.E2SelectionSources,
        "iter_building_summaries",
        counted_summaries,
    )
    monkeypatch.setattr(
        source_module.E2SelectionSources,
        "iter_all_candidates",
        counted_all,
    )
    monkeypatch.setattr(
        source_module.E2SelectionSources,
        "iter_candidates",
        forbidden_per_building,
    )
    monkeypatch.setattr(
        source_module.E2SelectionSources,
        "direct_phash_pairs",
        forbidden_global_edges,
    )
    monkeypatch.setattr(
        source_module.E2SelectionSources,
        "iter_same_building_direct_phash_edges",
        counted_same_building_edges,
    )

    report = validate_e3_artifact(artifact)

    assert report.passed, report.failed_check_names
    assert calls == {
        "summaries": 1,
        "all_candidates": 1,
        "per_building": 0,
        "global_edges": 0,
        "same_building_edges": 1,
    }
    assert _check(report, "validator_selection_mode_supported").passed
    assert _check(report, "selected_building_full_population_source_and_hashes").passed
    assert _check(report, "full_streaming_checkpoints").passed
    assert _check(report, "p2_direct_suppression_e2_edge_provenance").passed
    assert _check(report, "e2_full_byte_sha_start_manifest_end").passed


def test_full_mode_detects_candidate_and_checkpoint_tamper(tmp_path: Path) -> None:
    artifact, _e2 = _build_valid_fixture(
        tmp_path / "full-tamper-base",
        selection_mode="full",
    )
    candidate_tamper = _copy_fixture(artifact, tmp_path / "full-candidate-tamper.db")
    _drop_update_guard_and_mutate(
        candidate_tamper,
        "image_candidates",
        "UPDATE image_candidates SET canonical_url=canonical_url||'?tampered=1' "
        "WHERE candidate_id=(SELECT min(candidate_id) FROM image_candidates)",
    )
    checkpoint_tamper = _copy_fixture(artifact, tmp_path / "full-checkpoint-tamper.db")
    _drop_update_guard_and_mutate(
        checkpoint_tamper,
        "build_checkpoints",
        "UPDATE build_checkpoints SET completed_rows=completed_rows+1 "
        "WHERE phase='candidates'",
    )

    candidate_report = validate_e3_artifact(candidate_tamper)
    checkpoint_report = validate_e3_artifact(checkpoint_tamper)

    assert not candidate_report.passed
    assert not _check(
        candidate_report, "candidate_e2_fields_relations_and_record_hashes"
    ).passed
    assert not checkpoint_report.passed
    assert not _check(checkpoint_report, "full_streaming_checkpoints").passed


def test_full_mode_detects_every_terminal_checkpoint_cursor_tamper(
    tmp_path: Path,
) -> None:
    artifact, _e2 = _build_valid_fixture(
        tmp_path / "full-cursor-tamper-base",
        selection_mode="full",
    )
    connection = sqlite3.connect(artifact)
    cursors = {
        str(phase): json.loads(str(cursor_json))
        for phase, cursor_json in connection.execute(
            "SELECT phase,cursor_json FROM build_checkpoints ORDER BY phase"
        )
    }
    connection.close()

    cases = (
        ("inventory-last-key", "inventory", "last_key"),
        ("inventory-population", "inventory", "population_counts"),
        ("inventory-eligible", "inventory", "eligible_counts"),
        ("selection-manifest", "selection", "ordered_manifest_sha256"),
        ("candidate-last-key", "candidates", "last_key"),
        ("candidate-ranking", "candidates", "ranking_rows"),
        ("candidate-shortlist", "candidates", "shortlist_rows"),
        ("candidate-direct-edges", "candidates", "direct_edge_rows"),
    )
    for label, phase, field in cases:
        tampered = _copy_fixture(artifact, tmp_path / f"{label}.db")
        payload = json.loads(json.dumps(cursors[phase]))
        if field == "last_key":
            payload[field] = ["divisare", "not-an-e2-building"]
        elif field in {"population_counts", "eligible_counts"}:
            first = sorted(payload[field])[0]
            payload[field][first] += 1
        elif field == "ordered_manifest_sha256":
            payload[field] = "0" * 64
        else:
            payload[field] += 1
        _drop_update_guard_and_mutate(
            tampered,
            "build_checkpoints",
            "UPDATE build_checkpoints SET cursor_json=? WHERE phase=?",
            (canonical_json(payload), phase),
        )

        report = validate_e3_artifact(tampered)

        assert not report.passed, label
        assert not _check(report, "full_streaming_checkpoints").passed, label


def test_full_mode_detects_p2_direct_edge_detail_tamper(tmp_path: Path) -> None:
    artifact, _e2 = _build_valid_fixture(
        tmp_path / "full-edge-detail-tamper-base",
        selection_mode="full",
    )
    connection = sqlite3.connect(artifact)
    candidate_id, detail_json = connection.execute(
        """
        SELECT candidate_id,detail_json FROM policy_rankings
        WHERE policy_id='p2_quality_exact_direct_phash_shortlist'
          AND suppression_reason='suppressed_direct_phash_le8'
        ORDER BY selection_id,candidate_id LIMIT 1
        """
    ).fetchone()
    connection.close()
    original_detail = json.loads(str(detail_json))
    assert original_detail["direct_phash_edge"] is not None

    replacements = {
        "edge_id": "tampered-edge-id",
        "distance": int(original_detail["direct_phash_edge"]["distance"]) + 1,
        "edge_record_sha256": "0" * 64,
    }
    for field, value in replacements.items():
        tampered = _copy_fixture(artifact, tmp_path / f"edge-detail-{field}.db")
        detail = json.loads(json.dumps(original_detail))
        detail["direct_phash_edge"][field] = value
        _drop_update_guard_and_mutate(
            tampered,
            "policy_rankings",
            """
            UPDATE policy_rankings SET detail_json=?
            WHERE policy_id='p2_quality_exact_direct_phash_shortlist'
              AND candidate_id=?
            """,
            (canonical_json(detail), str(candidate_id)),
        )

        report = validate_e3_artifact(tampered)

        assert not report.passed, field
        assert not _check(
            report, "p2_direct_suppression_e2_edge_provenance"
        ).passed, field
        assert _check(
            report, "p0_p1_p2_rankings_and_nontransitive_chosen_star"
        ).passed, field


def test_full_mode_final_byte_rehash_detects_same_size_e2_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact, e2_path = _build_valid_fixture(
        tmp_path / "full-final-rehash",
        selection_mode="full",
    )
    original = validator_module._validate_full_candidates_and_rankings
    original_size = e2_path.stat().st_size
    mutated = False

    def validate_then_mutate(*args, **kwargs):
        nonlocal mutated
        result = original(*args, **kwargs)
        with e2_path.open("r+b") as stream:
            stream.seek(-1, 2)
            value = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes((value[0] ^ 1,)))
        mutated = True
        return result

    monkeypatch.setattr(
        validator_module,
        "_validate_full_candidates_and_rankings",
        validate_then_mutate,
    )

    report = validate_e3_artifact(artifact)

    assert mutated
    assert e2_path.stat().st_size == original_size
    assert not report.passed
    assert not _check(report, "e2_full_byte_sha_start_manifest_end").passed
    assert not _check(report, "e2_unchanged_after_full_validation_read").passed
    assert not report.input_files[0].passed


def test_validator_cli_accepts_full_fixture(tmp_path: Path, capsys) -> None:
    artifact, _e2 = _build_valid_fixture(
        tmp_path / "full-cli",
        selection_mode="full",
    )

    exit_code = validator_cli_main([str(artifact), "--json", "--compact"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["validator_version"].endswith("-v2")


def test_immutable_open_does_not_create_output_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "immutable.db"
    _empty_schema(path)

    connection = open_immutable(path)
    assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    connection.close()

    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    assert not Path(str(path) + "-journal").exists()


def test_validator_does_not_import_or_call_pipeline() -> None:
    tree = ast.parse(inspect.getsource(validator_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "canonical.cross_source_image_selection_pipeline" not in imported
    assert all("pipeline" not in name for name in imported)
