from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import sqlite3
import struct
from pathlib import Path

import pytest
from PIL import Image

import canonical.cross_source_semantic_vision_n100_sidecar as n100_runtime
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage_n100 import (
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
)
from tools.inspect_cross_source_semantic_vision_n100 import (
    create_ledger_template,
    freeze_ledger,
    inspect_frozen_ledger,
)
from tools.render_cross_source_semantic_review_sheets_n100 import (
    render_n100_review_sheets,
)


def _jpeg(color: tuple[int, int, int]) -> tuple[bytes, str]:
    image = Image.new("RGB", (140, 100), color)
    output = io.BytesIO()
    image.save(
        output,
        "JPEG",
        quality=92,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    encoded = output.getvalue()
    with Image.open(io.BytesIO(encoded)) as delivered:
        delivered.load()
        rgb = delivered.convert("RGB")
    pixel = hashlib.sha256(
        b"RGB\0" + struct.pack(">II", rgb.width, rgb.height) + rgb.tobytes()
    ).hexdigest()
    return encoded, pixel


def _semantic_db(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "semantic-n100.db"
    cache = tmp_path / "review-cache"
    cache.mkdir()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE semantic_runs(
              run_id TEXT, status TEXT, runner_version TEXT,
              contract_version TEXT, prompt_version TEXT,
              output_schema_sha256 TEXT, transform_version TEXT,
              model TEXT, reasoning TEXT, service_tier TEXT,
              logical_sha256 TEXT, building_count INTEGER,
              occurrence_count INTEGER, metrics_json TEXT
            );
            CREATE TABLE selected_buildings(
              run_id TEXT, building_rank INTEGER, selection_id TEXT,
              source TEXT, population_stratum TEXT, qa_fallback INTEGER,
              manifest_json TEXT
            );
            CREATE TABLE selected_occurrences(
              run_id TEXT, input_rank INTEGER, inference_id TEXT,
              selection_id TEXT, source TEXT, manifest_json TEXT
            );
            CREATE TABLE vision_inputs(
              run_id TEXT, inference_id TEXT, status TEXT,
              derivative_encoded_sha256 TEXT, derivative_pixel_sha256 TEXT,
              derivative_width INTEGER, derivative_height INTEGER,
              derivative_bytes INTEGER
            );
            CREATE TABLE semantic_results(
              run_id TEXT, inference_id TEXT, in_scope INTEGER,
              reject_reason TEXT, medium TEXT, spatial_context TEXT,
              framing_scale TEXT, camera_angle TEXT, drawing_kind TEXT,
              project_state TEXT, project_legibility TEXT,
              uncertain_axes_json TEXT, resolution_insufficient INTEGER,
              evidence TEXT
            );
            CREATE TABLE occurrence_result_links(
              run_id TEXT, inference_id TEXT, result_inference_id TEXT
            );
            CREATE TABLE hero_candidate_decisions(
              run_id TEXT, inference_id TEXT, tier TEXT
            );
            CREATE TABLE fetch_attempts(run_id TEXT);
            CREATE TABLE vision_attempts(
              run_id TEXT, input_tokens INTEGER,
              cached_input_tokens INTEGER, output_tokens INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO semantic_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "semn100-fixture",
                "complete",
                "runner-v1",
                "contract-v1",
                "prompt-v1",
                "a" * 64,
                "transform-v1",
                "model-v1",
                "low",
                "fast",
                "b" * 64,
                100,
                2,
                canonical_json(
                    {
                        "materialization_cache": {
                            "path": str(cache.resolve()),
                            "retained_for_review": True,
                            "write_contract": "atomic-exclusive-sha256-v1",
                        }
                    }
                ),
            ),
        )
        for rank in range(1, 101):
            source = "architizer" if rank <= 67 else "divisare"
            stratum = "ordinary" if rank % 7 else "cross_source_candidate"
            building_manifest = canonical_json(
                {
                    "selected_building": {
                        "building": {"successful_gallery_count": 8 + (rank % 20)}
                    }
                }
            )
            connection.execute(
                "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?)",
                (
                    "semn100-fixture",
                    rank,
                    f"building-{rank:03d}",
                    source,
                    stratum,
                    0,
                    building_manifest,
                ),
            )
        model_rows = [
            {
                "camera_angle": "eye_level",
                "drawing_kind": "not_applicable",
                "evidence": "A complete exterior building is clearly visible.",
                "framing_scale": "overall",
                "in_scope": 1,
                "medium": "photograph",
                "project_legibility": "high",
                "project_state": "visibly_finished",
                "reject_reason": "none",
                "resolution_insufficient": 0,
                "spatial_context": "exterior",
                "uncertain_axes_json": "[]",
            },
            {
                "camera_angle": "eye_level",
                "drawing_kind": "not_applicable",
                "evidence": "A complete interior room is clearly visible.",
                "framing_scale": "overall",
                "in_scope": 1,
                "medium": "photograph",
                "project_legibility": "high",
                "project_state": "visibly_finished",
                "reject_reason": "none",
                "resolution_insufficient": 0,
                "spatial_context": "interior",
                "uncertain_axes_json": "[]",
            },
        ]
        origins = [
            ["representative_p2_rank_1", "coverage_anchor_p1_rank_1"],
            ["coverage_probe"],
        ]
        for rank, model in enumerate(model_rows, 1):
            inference_id = f"semv100_{rank:06d}"
            encoded, pixel = _jpeg((rank * 60, rank * 50, rank * 40))
            (cache / f"{inference_id}.jpg").write_bytes(encoded)
            occurrence_manifest = canonical_json(
                {"occurrence": {"origins": origins[rank - 1]}}
            )
            connection.execute(
                "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?)",
                (
                    "semn100-fixture",
                    rank,
                    inference_id,
                    "building-001",
                    "architizer",
                    occurrence_manifest,
                ),
            )
            connection.execute(
                "INSERT INTO vision_inputs VALUES(?,?,?,?,?,?,?,?)",
                (
                    "semn100-fixture",
                    inference_id,
                    "success",
                    hashlib.sha256(encoded).hexdigest(),
                    pixel,
                    140,
                    100,
                    len(encoded),
                ),
            )
            connection.execute(
                """
                INSERT INTO semantic_results VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    "semn100-fixture",
                    inference_id,
                    model["in_scope"],
                    model["reject_reason"],
                    model["medium"],
                    model["spatial_context"],
                    model["framing_scale"],
                    model["camera_angle"],
                    model["drawing_kind"],
                    model["project_state"],
                    model["project_legibility"],
                    model["uncertain_axes_json"],
                    model["resolution_insufficient"],
                    model["evidence"],
                ),
            )
            connection.execute(
                "INSERT INTO occurrence_result_links VALUES(?,?,?)",
                ("semn100-fixture", inference_id, inference_id),
            )
            connection.execute(
                "INSERT INTO hero_candidate_decisions VALUES(?,?,?)",
                ("semn100-fixture", inference_id, "preferred"),
            )
            connection.execute(
                "INSERT INTO fetch_attempts VALUES(?)", ("semn100-fixture",)
            )
        connection.execute(
            "INSERT INTO vision_attempts VALUES(?,?,?,?)",
            ("semn100-fixture", 1234, 100, 56),
        )
        connection.commit()
    finally:
        connection.close()
    return database, cache


def _complete_draft(path: Path) -> None:
    draft = json.loads(path.read_text(encoding="utf-8"))
    draft["blind_review_completed_before_unblinding"] = True
    by_id = {row["inference_id"]: row for row in draft["rows"]}
    first = by_id["semv100_000001"]
    first_primary = {
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "framing_scale": "overall",
        "in_scope": True,
        "medium": "photograph",
        "project_legibility": "high",
        "project_state": "visibly_finished",
        "reject_reason": "none",
        "spatial_context": "exterior",
    }
    first["primary"] = first_primary
    first["acceptable_values"] = {
        key: [value] for key, value in first_primary.items()
    }
    first["acceptable_values"]["medium"] = ["photograph", "rendering"]
    first["expected_uncertain_axes"] = ["medium"]
    first["resolution_insufficient"] = False
    first["reason"] = "The visible image is a clear finished exterior photograph."

    second = by_id["semv100_000002"]
    second_primary = {
        "camera_angle": "not_applicable",
        "drawing_kind": "not_applicable",
        "framing_scale": "not_applicable",
        "in_scope": False,
        "medium": "photograph",
        "project_legibility": "none",
        "project_state": "not_applicable",
        "reject_reason": "no_project_visible",
        "spatial_context": "not_applicable",
    }
    second["primary"] = second_primary
    second["acceptable_values"] = {
        key: [value] for key, value in second_primary.items()
    }
    second["expected_uncertain_axes"] = ["scope"]
    second["resolution_insufficient"] = False
    second["reason"] = "No coherent architectural project is dominant in the frame."
    path.write_text(canonical_json(draft) + "\n", encoding="utf-8")


def _frozen_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    database, cache = _semantic_db(tmp_path)
    sheets = tmp_path / "sheets"
    render_n100_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=sheets,
    )
    review_manifest = sheets / "manifest.json"
    draft = tmp_path / "review-draft.json"
    create_ledger_template(
        review_manifest_path=review_manifest,
        output_path=draft,
        reviewer_id="codex-agent-reviewer",
        created_at="2026-08-12T00:00:00Z",
    )
    _complete_draft(draft)
    frozen = tmp_path / "review-frozen.json"
    freeze_ledger(
        review_manifest_path=review_manifest,
        draft_path=draft,
        output_path=frozen,
        completed_at="2026-08-12T01:00:00Z",
    )
    return database, review_manifest, draft, frozen


def test_template_freeze_and_immutable_inspection_metrics(tmp_path: Path) -> None:
    database, review_manifest, _, frozen = _frozen_fixture(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    result = inspect_frozen_ledger(
        semantic_db=database,
        review_manifest_path=review_manifest,
        ledger_path=frozen,
    )

    assert result["accounting"] == {
        "buildings": 100,
        "frozen_reference_rows": 2,
        "missing_result_rows": 0,
        "occurrences": 2,
        "reviewed_occurrences": 2,
        "semantic_results": 2,
        "unrendered_occurrences": 0,
    }
    assert result["fields"]["medium"]["acceptable_agreement"] == 1.0
    assert result["fields"]["in_scope"]["acceptable_agreement"] == 0.5
    assert result["uncertainty"]["axes"]["medium"]["fn"] == 1
    assert result["uncertainty"]["axes"]["scope"]["fn"] == 1
    assert result["uncertainty"]["predicted_near_zero_while_reference_material"]
    assert len(result["hero"]["critical_false_positive_rows"]) == 2
    assert result["coverage"]["model"]["total_new_building_slot_pairs"] == 1
    assert result["coverage"]["reviewer_primary"][
        "total_new_building_slot_pairs"
    ] == 0
    assert result["runtime"]["input_tokens"] == 1234
    assert result["reference"]["type"] == "agent_reference"
    assert result["operations"] == {
        "db_writes": 0,
        "llm_requests": 0,
        "network_requests": 0,
        "vision_requests": 0,
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_draft_cannot_join_and_freeze_is_complete_and_no_clobber(tmp_path: Path) -> None:
    database, review_manifest, draft, frozen = _frozen_fixture(tmp_path)
    with pytest.raises(ValueError, match="only be joined to a frozen ledger"):
        inspect_frozen_ledger(
            semantic_db=tmp_path / "not-opened.db",
            review_manifest_path=review_manifest,
            ledger_path=draft,
        )

    with pytest.raises(FileExistsError):
        freeze_ledger(
            review_manifest_path=review_manifest,
            draft_path=draft,
            output_path=frozen,
        )
    with pytest.raises(FileExistsError):
        create_ledger_template(
            review_manifest_path=review_manifest,
            output_path=draft,
            reviewer_id="another-reviewer",
        )

    incomplete = json.loads(draft.read_text(encoding="utf-8"))
    incomplete["rows"][0]["reason"] = ""
    incomplete_path = tmp_path / "incomplete.json"
    incomplete_path.write_text(canonical_json(incomplete) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reason is required"):
        freeze_ledger(
            review_manifest_path=review_manifest,
            draft_path=incomplete_path,
            output_path=tmp_path / "should-not-exist.json",
        )
    assert not (tmp_path / "should-not-exist.json").exists()
    assert database.is_file()


def test_tampered_frozen_ledger_is_rejected(tmp_path: Path) -> None:
    database, review_manifest, _, frozen = _frozen_fixture(tmp_path)
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    payload["rows"][0]["reason"] += " tampered"
    frozen.write_text(canonical_json(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frozen ledger SHA mismatch"):
        inspect_frozen_ledger(
            semantic_db=database,
            review_manifest_path=review_manifest,
            ledger_path=frozen,
        )


def test_complete_with_failures_preserves_unrendered_occurrence_accounting(
    tmp_path: Path,
) -> None:
    database, cache = _semantic_db(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE semantic_runs SET status='complete_with_failures',occurrence_count=3"
        )
        connection.execute(
            "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?)",
            (
                "semn100-fixture",
                3,
                "semv100_000003",
                "building-002",
                "architizer",
                canonical_json({"occurrence": {"origins": ["coverage_probe"]}}),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    sheets = tmp_path / "failure-sheets"
    render_n100_review_sheets(
        semantic_db=database,
        review_cache_dir=cache,
        output_dir=sheets,
    )
    draft = tmp_path / "failure-draft.json"
    create_ledger_template(
        review_manifest_path=sheets / "manifest.json",
        output_path=draft,
        reviewer_id="failure-accounting-reviewer",
    )
    _complete_draft(draft)
    frozen = tmp_path / "failure-frozen.json"
    freeze_ledger(
        review_manifest_path=sheets / "manifest.json",
        draft_path=draft,
        output_path=frozen,
    )
    result = inspect_frozen_ledger(
        semantic_db=database,
        review_manifest_path=sheets / "manifest.json",
        ledger_path=frozen,
    )
    assert result["accounting"]["occurrences"] == 3
    assert result["accounting"]["reviewed_occurrences"] == 2
    assert result["accounting"]["unrendered_occurrences"] == 1
    assert result["unrendered_occurrence_inference_ids"] == ["semv100_000003"]


def _load_runtime_fixture_helpers():
    """Load the real N100 runner's offline fixture without importing test state."""

    path = Path(__file__).with_name(
        "test_cross_source_semantic_vision_n100_sidecar.py"
    )
    spec = importlib.util.spec_from_file_location("_n100_runtime_fixture_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_n100_runtime_review_cache_to_frozen_inspection_integration(
    tmp_path: Path,
) -> None:
    helpers = _load_runtime_fixture_helpers()
    args, images, _ = helpers._run_args(tmp_path)

    # Enrich only the synthetic fixture's manifest with fields present in the
    # production manifest and needed by the post-run coverage inspector.
    manifest_path = args["manifest_path"]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("semantic_coverage_manifest_sha256", None)
    for wrapper in payload["selected_buildings"]:
        selected = wrapper["selected_building"]
        selected["building"]["successful_gallery_count"] = 2
        for occurrence in selected["coverage_plan"]["selected_occurrences"]:
            occurrence["occurrence"]["origins"] = [
                "representative_p2_rank_1",
                "coverage_anchor_p1_rank_1",
            ]
    payload["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {
            "domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
            "manifest": payload,
        }
    )
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    manifest_path.write_bytes(raw)
    args["contract"] = dataclasses.replace(
        args["contract"],
        manifest_byte_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_self_sha256=payload["semantic_coverage_manifest_sha256"],
    )
    review_cache = tmp_path / "retained-review-cache"
    args["review_cache_dir"] = review_cache

    result = n100_runtime.run_semantic_vision_n100(
        **args,
        fetcher=helpers._fetcher(images),
        executor=lambda **kwargs: helpers._vision_result(kwargs, ok=True),
    )
    assert result.status == "complete"
    assert len(list(review_cache.glob("semv100_*.jpg"))) == 100
    connection = sqlite3.connect(args["output_db"])
    try:
        metrics = json.loads(
            connection.execute("SELECT metrics_json FROM semantic_runs").fetchone()[0]
        )
    finally:
        connection.close()
    assert metrics["materialization_cache"]["retained_for_review"] is True
    assert Path(metrics["materialization_cache"]["path"]) == review_cache.resolve()

    sheets = tmp_path / "blind-sheets"
    rendered = render_n100_review_sheets(
        semantic_db=args["output_db"],
        review_cache_dir=review_cache,
        output_dir=sheets,
    )
    assert rendered["input_count"] == 100
    assert rendered["sheet_count"] == 17
    assert all(value.startswith("semv100_") for value in rendered["ordered_inference_ids"])

    draft = tmp_path / "draft.json"
    created = create_ledger_template(
        review_manifest_path=sheets / "manifest.json",
        output_path=draft,
        reviewer_id="offline-agent-reference",
        created_at="2026-08-12T02:00:00Z",
    )
    assert created["semantic_labels_read"] is False
    assert all(
        all(value is None for value in row["primary"].values())
        for row in created["rows"]
    )

    edited = json.loads(draft.read_text(encoding="utf-8"))
    edited["blind_review_completed_before_unblinding"] = True
    primary = {
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "framing_scale": "overall",
        "in_scope": True,
        "medium": "photograph",
        "project_legibility": "high",
        "project_state": "visibly_finished",
        "reject_reason": "none",
        "spatial_context": "exterior",
    }
    for row in edited["rows"]:
        row["primary"] = dict(primary)
        row["acceptable_values"] = {
            field: [value] for field, value in primary.items()
        }
        row["expected_uncertain_axes"] = []
        row["resolution_insufficient"] = False
        row["reason"] = "A coherent finished building facade is visibly dominant."
    draft.write_text(canonical_json(edited) + "\n", encoding="utf-8")
    frozen = tmp_path / "frozen.json"
    freeze_ledger(
        review_manifest_path=sheets / "manifest.json",
        draft_path=draft,
        output_path=frozen,
        completed_at="2026-08-12T03:00:00Z",
    )
    inspection = inspect_frozen_ledger(
        semantic_db=args["output_db"],
        review_manifest_path=sheets / "manifest.json",
        ledger_path=frozen,
    )
    assert inspection["accounting"]["buildings"] == 100
    assert inspection["accounting"]["reviewed_occurrences"] == 100
    assert inspection["accounting"]["unrendered_occurrences"] == 0
    assert inspection["fields"]["in_scope"]["acceptable_agreement"] == 1.0
    assert inspection["hero"]["disagreement_count"] == 0
    assert inspection["operations"]["network_requests"] == 0
    assert inspection["operations"]["vision_requests"] == 0
