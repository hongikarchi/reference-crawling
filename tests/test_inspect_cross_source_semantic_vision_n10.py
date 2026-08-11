from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)
from tools.inspect_cross_source_semantic_vision_n10 import (
    _write_no_clobber,
    inspect_n10,
)


def _write_manifest(path: Path, *, ordered_occurrence_sha: str = "occurrences") -> dict[str, object]:
    payload: dict[str, object] = {
        "manifest_version": "fixture-v1",
        "ordered_building_manifest_sha256": "buildings",
        "ordered_occurrence_manifest_sha256": ordered_occurrence_sha,
    }
    payload["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": payload}
    )
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8", newline="\n")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_fixture(tmp_path: Path) -> tuple[Path, Path]:
    manifest_path = tmp_path / "semantic_n10_manifest.json"
    manifest = _write_manifest(manifest_path)
    manifest_byte_sha = _sha256(manifest_path)
    db_path = tmp_path / "semantic_n10.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE semantic_runs(
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            manifest_byte_sha256 TEXT NOT NULL,
            manifest_self_sha256 TEXT NOT NULL,
            ordered_building_manifest_sha256 TEXT NOT NULL,
            ordered_occurrence_manifest_sha256 TEXT NOT NULL,
            building_count INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL,
            logical_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL
        );
        CREATE TABLE selected_buildings(
            run_id TEXT NOT NULL,
            building_rank INTEGER NOT NULL,
            selection_id TEXT NOT NULL,
            source TEXT NOT NULL,
            population_stratum TEXT NOT NULL,
            guard_name TEXT NOT NULL,
            qa_fallback INTEGER NOT NULL,
            manifest_json TEXT NOT NULL
        );
        CREATE TABLE selected_occurrences(
            run_id TEXT NOT NULL,
            input_rank INTEGER NOT NULL,
            inference_id TEXT NOT NULL,
            selection_id TEXT NOT NULL,
            occurrence_rank INTEGER NOT NULL,
            source TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        );
        CREATE TABLE vision_inputs(
            inference_id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        );
        CREATE TABLE fetch_attempts(
            inference_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            response_bytes INTEGER NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE vision_attempts(
            attempt_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            inference_ids_json TEXT NOT NULL
        );
        CREATE TABLE semantic_results(
            inference_id TEXT PRIMARY KEY,
            in_scope INTEGER NOT NULL,
            reject_reason TEXT NOT NULL,
            medium TEXT NOT NULL,
            spatial_context TEXT NOT NULL,
            framing_scale TEXT NOT NULL,
            camera_angle TEXT NOT NULL,
            drawing_kind TEXT NOT NULL,
            project_state TEXT NOT NULL,
            project_legibility TEXT NOT NULL,
            uncertain_axes_json TEXT NOT NULL,
            resolution_insufficient INTEGER NOT NULL,
            evidence TEXT NOT NULL
        );
        CREATE TABLE hero_candidate_decisions(
            inference_id TEXT PRIMARY KEY,
            tier TEXT NOT NULL
        );
        CREATE TABLE coverage_slot_assignments(
            selection_id TEXT NOT NULL,
            slot TEXT NOT NULL,
            assignment_rank INTEGER NOT NULL,
            state TEXT NOT NULL,
            inference_id TEXT
        );
        CREATE TABLE validations(
            validation_name TEXT PRIMARY KEY,
            severity TEXT NOT NULL,
            passed INTEGER NOT NULL,
            expected TEXT,
            actual TEXT,
            detail TEXT
        );
        """
    )
    run_id = "fixture-run"
    connection.execute(
        """
        INSERT INTO semantic_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            "complete",
            manifest_byte_sha,
            manifest["semantic_coverage_manifest_sha256"],
            manifest["ordered_building_manifest_sha256"],
            manifest["ordered_occurrence_manifest_sha256"],
            10,
            57,
            "logical-fixture",
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:04:00Z",
            "contract-v1",
            "model-v1",
            "prompt-v1",
        ),
    )
    inference_ids: list[str] = []
    input_rank = 0
    for building_index in range(1, 11):
        source = "architizer" if building_index <= 5 else "divisare"
        selection_id = f"{source}:building:b{building_index:02d}"
        building_manifest = canonical_json(
            {
                "selected_building": {
                    "building": {"name": f"Building {building_index:02d}"}
                }
            }
        )
        connection.execute(
            "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                building_index,
                selection_id,
                source,
                "ordinary" if building_index % 2 else "coverage_risk",
                "ordinary",
                int(building_index == 1),
                building_manifest,
            ),
        )
        occurrence_count = 6 if building_index <= 7 else 5
        for occurrence_rank in range(1, occurrence_count + 1):
            input_rank += 1
            inference_id = f"semv_{input_rank:06d}"
            inference_ids.append(inference_id)
            origins = []
            if occurrence_rank <= 3:
                origins.append(f"representative_p2_rank_{occurrence_rank}")
            if occurrence_rank == 1:
                origins.append("coverage_anchor_p1_rank_1")
            if occurrence_rank >= 4:
                origins.append("coverage_probe")
            occurrence_manifest = canonical_json(
                {"occurrence": {"origins": origins}}
            )
            connection.execute(
                "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    input_rank,
                    inference_id,
                    selection_id,
                    occurrence_rank,
                    source,
                    occurrence_manifest,
                ),
            )
            connection.execute(
                "INSERT INTO vision_inputs VALUES(?,?)", (inference_id, "success")
            )
            connection.execute(
                "INSERT INTO fetch_attempts VALUES(?,?,?,?,?)",
                (inference_id, 1, 10, 100, "success"),
            )
            uncertain = input_rank == 2
            connection.execute(
                "INSERT INTO semantic_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    inference_id,
                    int(input_rank != 1),
                    "not_architecture" if input_rank == 1 else "none",
                    "drawing" if occurrence_rank == 4 else "photograph",
                    "exterior",
                    "detail" if occurrence_rank >= 3 else "overall",
                    "eye_level",
                    "plan" if occurrence_rank == 4 else "none",
                    "built",
                    "low" if uncertain else "high",
                    canonical_json(["project_legibility"] if uncertain else []),
                    int(uncertain),
                    f"Evidence {input_rank}",
                ),
            )
            tier = "rejected" if input_rank == 1 else "preferred"
            connection.execute(
                "INSERT INTO hero_candidate_decisions VALUES(?,?)",
                (inference_id, tier),
            )
            if occurrence_rank == 1:
                slots = ("exterior_overall",)
            elif occurrence_rank == 2:
                slots = ("interior",)
            elif occurrence_rank == 3:
                slots = ("detail",)
            elif occurrence_rank == 4:
                slots = ("drawing_plan",)
            else:
                slots = ()
            for slot in slots:
                connection.execute(
                    "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?)",
                    (selection_id, slot, 1, "observed", inference_id),
                )
    assert input_rank == 57
    for attempt_id, offset in enumerate(range(0, len(inference_ids), 5), start=1):
        batch = inference_ids[offset : offset + 5]
        connection.execute(
            "INSERT INTO vision_attempts VALUES(?,?,?,?,?,?,?)",
            (attempt_id, "success", 1000, 100, 10, 20, canonical_json(batch)),
        )
    connection.execute(
        "INSERT INTO validations VALUES(?,?,?,?,?,?)",
        ("fixture", "error", 1, "pass", "pass", None),
    )
    connection.commit()
    connection.close()
    return db_path, manifest_path


def test_inspector_reports_fixed_n10_aggregates_without_writes(tmp_path: Path) -> None:
    db_path, manifest_path = _create_fixture(tmp_path)
    db_sha_before = _sha256(db_path)

    report = inspect_n10(db_path, manifest_path)

    assert report["accounting"] == {
        "buildings": 10,
        "missing_hero_decisions": 0,
        "missing_semantic_results": 0,
        "occurrences": 57,
        "orphan_hero_decisions": 0,
        "orphan_semantic_results": 0,
        "results": 57,
        "successful_inputs": 57,
    }
    assert report["hero"]["tier_distribution"] == {
        "preferred": 56,
        "rejected": 1,
    }
    assert report["uncertainty"]["axis_distribution"] == {
        "project_legibility": 1
    }
    assert report["uncertainty"]["resolution_insufficient_count"] == 1
    comparison = report["coverage"]["p2_top3_vs_expanded"]
    assert comparison["buildings_with_new_slots"] == 10
    assert comparison["new_slot_building_distribution"] == {"drawing_plan": 10}
    assert comparison["total_new_building_slot_pairs"] == 10
    anchors = report["non_qa_p1_anchor"]
    assert anchors["count"] == 9
    assert anchors["in_scope_count"] == 9
    assert anchors["missing_hero_decision_count"] == 0
    assert anchors["missing_result_count"] == 0
    assert report["runtime"]["model_calls"] == 12
    assert report["runtime"]["downloaded_bytes"] == 5_700
    assert report["runtime"]["input_tokens"] == 1_200
    assert report["runtime"]["output_tokens"] == 240
    assert report["n100_projection"]["projected_images"] == 570
    assert report["n100_projection"]["projected_model_calls"] == 120
    assert report["validation"]["quick_check"] == "ok"
    assert report["validation"]["integrity_check"] == "ok"
    assert report["validation"]["foreign_key_violations"] == 0
    assert report["validation"]["inputs_unchanged_and_no_db_sidecars"] is True
    assert report["operations"] == {
        "db_writes": 0,
        "llm_requests": 0,
        "network_requests": 0,
        "vision_requests": 0,
    }
    assert _sha256(db_path) == db_sha_before
    assert not Path(str(db_path) + "-wal").exists()
    assert not Path(str(db_path) + "-shm").exists()
    assert not Path(str(db_path) + "-journal").exists()


def test_complete_with_failures_accounts_for_missing_result_and_hero(
    tmp_path: Path,
) -> None:
    db_path, manifest_path = _create_fixture(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE semantic_runs SET status='complete_with_failures'"
    )
    connection.execute(
        "UPDATE vision_inputs SET status='failed' WHERE inference_id='semv_000007'"
    )
    connection.execute(
        "DELETE FROM semantic_results WHERE inference_id='semv_000007'"
    )
    connection.execute(
        "DELETE FROM hero_candidate_decisions WHERE inference_id='semv_000007'"
    )
    connection.commit()
    connection.close()

    report = inspect_n10(db_path, manifest_path)

    assert report["run"]["status"] == "complete_with_failures"
    assert report["accounting"]["results"] == 56
    assert report["accounting"]["successful_inputs"] == 56
    assert report["accounting"]["missing_semantic_results"] == 1
    assert report["accounting"]["missing_hero_decisions"] == 1
    assert report["accounting"]["orphan_semantic_results"] == 0
    assert report["accounting"]["orphan_hero_decisions"] == 0
    assert report["missing"] == {
        "hero_decision_inference_ids": ["semv_000007"],
        "orphan_hero_decision_inference_ids": [],
        "orphan_semantic_result_inference_ids": [],
        "semantic_result_inference_ids": ["semv_000007"],
    }
    assert report["field_distributions"]["in_scope"]["missing"] == 1
    assert report["hero"]["tier_distribution"]["missing"] == 1
    anchors = report["non_qa_p1_anchor"]
    assert anchors["count"] == 9
    assert anchors["in_scope_count"] == 8
    assert anchors["missing_result_count"] == 1
    assert anchors["missing_hero_decision_count"] == 1
    missing_anchor = next(
        row for row in anchors["rows"] if row["inference_id"] == "semv_000007"
    )
    assert missing_anchor["result_status"] == "missing"
    assert missing_anchor["hero_tier"] == "missing"
    assert missing_anchor["in_scope"] is None
    assert missing_anchor["project_legibility"] is None


def test_inspector_rejects_manifest_lineage_mismatch(tmp_path: Path) -> None:
    db_path, manifest_path = _create_fixture(tmp_path)
    _write_manifest(manifest_path, ordered_occurrence_sha="tampered-lineage")

    with pytest.raises(ValueError, match="DB/manifest lineage mismatch"):
        inspect_n10(db_path, manifest_path)


def test_optional_json_output_is_no_clobber(tmp_path: Path) -> None:
    output = tmp_path / "inspection.json"
    payload = {"status": "complete"}

    _write_no_clobber(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    with pytest.raises(FileExistsError):
        _write_no_clobber(output, {"status": "replacement"})
    assert json.loads(output.read_text(encoding="utf-8")) == payload
