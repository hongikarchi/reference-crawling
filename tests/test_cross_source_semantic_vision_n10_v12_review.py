from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_vision_v12 import derive_hero_decision
from tools import inspect_cross_source_semantic_vision_n10_v12 as inspector
from tools.inspect_cross_source_semantic_vision_n10_v12 import (
    QA_FIELDS,
    create_independent_ledger_template,
    freeze_independent_ledger,
    inspect_reconciled_ledger,
)
from tools.reconcile_cross_source_semantic_review_n10_v12 import (
    MATERIAL_DIMENSIONS,
    create_reconciliation_template,
    freeze_reconciled_ledger,
    validate_reconciled_frozen_ledger,
)
from tools.render_cross_source_semantic_review_sheets import _pixel_identity
from tools.render_cross_source_semantic_review_sheets_n10_v12 import (
    PRODUCTION_PINS,
    ReviewPins,
    _file_sha,
    render_n10_v12_review_sheets,
)


SELECTION_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normal(inference_id: str, *, uncertain: bool = False) -> dict[str, object]:
    return {
        "asset_id": inference_id,
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "project_legibility": "high",
        "uncertain_axes": ["medium"] if uncertain else [],
        "resolution_insufficient": False,
        "evidence": "A coherent finished exterior is clearly visible in the pixels.",
    }


def _pins_and_selection(tmp_path: Path) -> tuple[ReviewPins, Path]:
    body: dict[str, object] = {
        "ordered_building_manifest_sha256": _sha("ordered-buildings-v12"),
        "ordered_occurrence_manifest_sha256": _sha("ordered-occurrences-v12"),
        "ordered_fetch_url_manifest_sha256": _sha("ordered-urls-v12"),
        "ordered_e1_pixel_manifest_sha256": _sha("ordered-pixels-v12"),
        "planned_occurrence_count": 50,
        "sample_size_buildings": 10,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SELECTION_DOMAIN, "manifest": body}
    )
    raw = (canonical_json(body) + "\n").encode()
    path = tmp_path / "selection.json"
    path.write_bytes(raw)
    pins = ReviewPins(
        selection_manifest_domain=SELECTION_DOMAIN,
        selection_manifest_size_bytes=len(raw),
        selection_manifest_byte_sha256=hashlib.sha256(raw).hexdigest(),
        selection_manifest_self_sha256=str(body["semantic_coverage_manifest_sha256"]),
        ordered_building_manifest_sha256=str(body["ordered_building_manifest_sha256"]),
        ordered_occurrence_manifest_sha256=str(body["ordered_occurrence_manifest_sha256"]),
        ordered_fetch_url_manifest_sha256=str(body["ordered_fetch_url_manifest_sha256"]),
        ordered_e1_pixel_manifest_sha256=str(body["ordered_e1_pixel_manifest_sha256"]),
        run_id="semn10v12-test-review",
        runner_version="cross-source-semantic-vision-n10-v1.2-runner-v2.0.0",
        schema_version=4,
        contract_version="cross-source-image-semantics-v1.2.0",
        prompt_version="cross-source-image-semantics-prompt-v1.2.0",
        output_schema_sha256=_sha("output-schema-v12"),
        schema_sql_sha256=_sha("schema-sql-v12"),
        schema_objects_sha256=_sha("schema-objects-v12"),
        transform_version="cross-source-vision-input-1024-jpeg-v1.0.0",
        e1_contract_version="archibe-e1-fingerprint-v1",
        dependency_manifest_sha256=_sha("dependency-manifest-v12"),
        runtime_version="divisare-codex-vision-runtime-v1.1.0",
        retry_policy_version="cross-source-semantic-vision-n10-v1.2-retry-v2.0.0",
        model="test-model",
        reasoning="low",
        service_tier="test",
        image_detail="high",
        cli_version="codex-cli 0.147.0",
        publication_contract_version="semantic-two-file-recoverable-publication-v2",
        prior_run_names_json=canonical_json([
            "semantic_n100_v1", "semantic_n10_v1", "semantic_n10_v1_1"
        ]),
        prior_successful_derivative_count=665,
        max_model_call_reservations=12,
        observed_input_token_stop_threshold=250_000,
        observed_output_token_stop_threshold=10_000,
        observed_total_token_stop_threshold=260_000,
    )
    return pins, path


def _build_fixture(
    tmp_path: Path,
    *,
    predicted_uncertain: set[int] | None = None,
    failed: set[int] | None = None,
):
    predicted_uncertain = predicted_uncertain or set()
    failed = failed or set()
    pins, selection = _pins_and_selection(tmp_path)
    cache = tmp_path / "review-cache"
    cache.mkdir()
    images: dict[str, tuple[str, str, int, int, int]] = {}
    for index in range(1, pins.occurrence_count + 1):
        inference_id = f"semv12_{index:06d}"
        path = cache / f"{inference_id}.jpg"
        Image.new(
            "RGB", (24, 18),
            ((index * 17) % 251, (index * 29) % 251, (index * 41) % 251),
        ).save(path, format="JPEG", quality=90)
        pixel_sha, width, height, image_format = _pixel_identity(path)
        assert image_format == "JPEG"
        images[inference_id] = (_file_sha(path), pixel_sha, width, height, path.stat().st_size)

    db = tmp_path / "semantic.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE semantic_runs(
          run_id TEXT,runner_version TEXT,schema_version INTEGER,
          contract_version TEXT,prompt_version TEXT,output_schema_sha256 TEXT,
          schema_sql_sha256 TEXT,schema_objects_sha256 TEXT,transform_version TEXT,
          e1_contract_version TEXT,dependency_manifest_sha256 TEXT,
          runtime_version TEXT,retry_policy_version TEXT,
          model TEXT,reasoning TEXT,service_tier TEXT,image_detail TEXT,cli_version TEXT,
          publication_contract_version TEXT,prior_run_names_json TEXT,
          prior_successful_derivative_count INTEGER,max_model_call_reservations INTEGER,
          observed_input_token_stop_threshold INTEGER,
          observed_output_token_stop_threshold INTEGER,
          observed_total_token_stop_threshold INTEGER,
          batch_size INTEGER,max_fetch_attempts INTEGER,max_vision_attempts INTEGER,
          manifest_byte_sha256 TEXT,manifest_domain TEXT,manifest_self_sha256 TEXT,
          ordered_building_manifest_sha256 TEXT,ordered_occurrence_manifest_sha256 TEXT,
          ordered_fetch_url_manifest_sha256 TEXT,ordered_e1_pixel_manifest_sha256 TEXT,
          inference_id_prefix TEXT,building_count INTEGER,occurrence_count INTEGER,
          status TEXT,metrics_json TEXT,logical_sha256 TEXT
        );
        CREATE TABLE selected_buildings(run_id TEXT,selection_id TEXT);
        CREATE TABLE selected_occurrences(run_id TEXT,input_rank INTEGER,inference_id TEXT);
        CREATE TABLE vision_inputs(
          run_id TEXT,inference_id TEXT,status TEXT,
          derivative_encoded_sha256 TEXT,derivative_pixel_sha256 TEXT,
          derivative_width INTEGER,derivative_height INTEGER,derivative_bytes INTEGER
        );
        CREATE TABLE semantic_results(
          run_id TEXT,inference_id TEXT,in_scope INTEGER,reject_reason TEXT,medium TEXT,
          spatial_context TEXT,framing_scale TEXT,camera_angle TEXT,drawing_kind TEXT,
          project_state TEXT,project_legibility TEXT,uncertain_axes_json TEXT,
          resolution_insufficient INTEGER,evidence TEXT
        );
        CREATE TABLE occurrence_result_links(
          run_id TEXT,inference_id TEXT,result_inference_id TEXT
        );
        CREATE TABLE hero_candidate_decisions(run_id TEXT,inference_id TEXT,tier TEXT);
        """
    )
    metrics = {
        "materialization_cache": {
            "path": str(cache.resolve()),
            "retained_for_review": True,
            "write_contract": "atomic-exclusive-sha256-v1",
        }
    }
    connection.execute(
        "INSERT INTO semantic_runs VALUES(" + ",".join("?" for _ in range(41)) + ")",
        (
            pins.run_id, pins.runner_version, pins.schema_version,
            pins.contract_version, pins.prompt_version, pins.output_schema_sha256,
            pins.schema_sql_sha256, pins.schema_objects_sha256, pins.transform_version,
            pins.e1_contract_version, pins.dependency_manifest_sha256,
            pins.runtime_version, pins.retry_policy_version,
            pins.model, pins.reasoning, pins.service_tier, pins.image_detail, pins.cli_version,
            pins.publication_contract_version, pins.prior_run_names_json,
            pins.prior_successful_derivative_count, pins.max_model_call_reservations,
            pins.observed_input_token_stop_threshold,
            pins.observed_output_token_stop_threshold,
            pins.observed_total_token_stop_threshold,
            pins.batch_size, pins.max_fetch_attempts, pins.max_vision_attempts,
            pins.selection_manifest_byte_sha256, pins.selection_manifest_domain,
            pins.selection_manifest_self_sha256,
            pins.ordered_building_manifest_sha256, pins.ordered_occurrence_manifest_sha256,
            pins.ordered_fetch_url_manifest_sha256, pins.ordered_e1_pixel_manifest_sha256,
            pins.inference_id_prefix, pins.building_count, pins.occurrence_count,
            "complete_with_failures" if failed else "complete",
            canonical_json(metrics), _sha("logical-v12"),
        ),
    )
    connection.executemany(
        "INSERT INTO selected_buildings VALUES(?,?)",
        [(pins.run_id, f"building-{index}") for index in range(1, 11)],
    )
    for index, (inference_id, image) in enumerate(images.items(), 1):
        semantic = _normal(inference_id, uncertain=index in predicted_uncertain)
        connection.execute("INSERT INTO selected_occurrences VALUES(?,?,?)", (pins.run_id, index, inference_id))
        connection.execute(
            "INSERT INTO vision_inputs VALUES(?,?,?,?,?,?,?,?)",
            (
                pins.run_id,
                inference_id,
                "vision_failed" if index in failed else "success",
                *image,
            ),
        )
        if index in failed:
            continue
        connection.execute(
            "INSERT INTO semantic_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pins.run_id, inference_id, int(bool(semantic["in_scope"])),
                semantic["reject_reason"], semantic["medium"], semantic["spatial_context"],
                semantic["framing_scale"], semantic["camera_angle"], semantic["drawing_kind"],
                semantic["project_state"], semantic["project_legibility"],
                canonical_json(semantic["uncertain_axes"]),
                int(bool(semantic["resolution_insufficient"])), semantic["evidence"],
            ),
        )
        connection.execute("INSERT INTO occurrence_result_links VALUES(?,?,?)", (pins.run_id, inference_id, inference_id))
        connection.execute("INSERT INTO hero_candidate_decisions VALUES(?,?,?)", (pins.run_id, inference_id, derive_hero_decision(semantic)[0]))
    connection.commit()
    connection.close()
    return pins, selection, db, cache


def _render(tmp_path: Path, pins: ReviewPins, selection: Path, db: Path, cache: Path):
    output = tmp_path / "review-sheets"
    manifest = render_n10_v12_review_sheets(
        semantic_db=db,
        selection_manifest=selection,
        review_cache_dir=cache,
        output_dir=output,
        pins=pins,
    )
    return output / "manifest.json", manifest


def _complete_independent(
    draft_path: Path,
    *,
    uncertain: set[int] | None = None,
    medium_override: dict[int, str] | None = None,
    resolution: set[int] | None = None,
) -> None:
    uncertain = uncertain or set()
    medium_override = medium_override or {}
    resolution = resolution or set()
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    payload["blind_review_completed_before_peer_exchange"] = True
    for row in payload["rows"]:
        source_index = int(str(row["inference_id"]).rsplit("_", 1)[1])
        semantic = _normal(
            row["inference_id"],
            uncertain=source_index in uncertain or source_index in resolution,
        )
        semantic["medium"] = medium_override.get(source_index, semantic["medium"])
        if semantic["medium"] == "mixed":
            for field in (
                "spatial_context", "framing_scale", "camera_angle",
                "drawing_kind", "project_state",
            ):
                semantic[field] = "not_applicable"
        semantic["resolution_insufficient"] = source_index in resolution
        row["primary"] = {field: semantic[field] for field in QA_FIELDS}
        row["acceptable_values"] = {field: [semantic[field]] for field in QA_FIELDS}
        row["expected_uncertain_axes"] = list(semantic["uncertain_axes"])
        row["resolution_insufficient"] = semantic["resolution_insufficient"]
        row["reason"] = semantic["evidence"]
    draft_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _two_frozen(tmp_path: Path, pins: ReviewPins, review_path: Path, *, disagree: bool = True):
    ledgers = []
    for label in ("a", "b"):
        draft = tmp_path / f"draft-{label}.json"
        frozen = tmp_path / f"frozen-{label}.json"
        create_independent_ledger_template(
            review_manifest_path=review_path,
            output_path=draft,
            reviewer_id=f"reviewer-{label}",
            created_at="2026-08-12T00:00:00Z",
            pins=pins,
        )
        _complete_independent(
            draft,
            uncertain={1, 2, 3, 4, 5},
            medium_override={6: "mixed"} if label == "b" and disagree else None,
            resolution={7} if label == "b" and disagree else None,
        )
        freeze_independent_ledger(
            review_manifest_path=review_path,
            draft_path=draft,
            output_path=frozen,
            completed_at="2026-08-12T00:10:00Z",
            pins=pins,
        )
        ledgers.append(frozen)
    return tuple(ledgers)


def _resolve(draft_path: Path) -> None:
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    payload["reconciliation_completed_before_unblinding"] = True
    payload["unresolved_dimension_count"] = 0
    for row in payload["rows"]:
        dimensions = list(row["disagreement_dimensions"])
        row["final"] = copy.deepcopy(row["reviewer_a"])
        row["resolved_dimensions"] = dimensions
        row["unresolved_dimensions"] = []
        row["pixel_grounded_resolutions"] = {
            dimension: (
                "Direct pixel comparison supports reviewer A for this visible boundary."
            )
            for dimension in dimensions
        }
    draft_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def test_production_pins_are_literal_exact_10_by_50_envelope() -> None:
    assert PRODUCTION_PINS is not None
    assert PRODUCTION_PINS.building_count == 10
    assert PRODUCTION_PINS.occurrence_count == 50
    assert PRODUCTION_PINS.run_id == "semn10v12-82ddad3e9cb37d3f640209c7"
    assert PRODUCTION_PINS.cli_version == "codex-cli 0.147.0"
    assert PRODUCTION_PINS.selection_manifest_byte_sha256 == (
        "00d494c58590a9a0ca3d78de858f857f6a1e226390018f5426d799a846ead8fe"
    )


def test_renderer_binds_exact_50_semv12_inputs_and_no_clobber(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, manifest = _render(tmp_path, pins, selection, db, cache)
    assert review_path.is_file()
    assert manifest["input_count"] == pins.occurrence_count == 50
    assert len(set(manifest["ordered_inference_ids"])) == pins.occurrence_count
    assert all(value.startswith("semv12_") for value in manifest["ordered_inference_ids"])
    assert manifest["selection_manifest"]["manifest_domain"] == SELECTION_DOMAIN
    assert manifest["semantic_run"]["schema_version"] == 4
    assert manifest["semantic_run"]["review_cache_provenance"]["retained_for_review"] is True
    with pytest.raises(FileExistsError):
        render_n10_v12_review_sheets(
            semantic_db=db, selection_manifest=selection, review_cache_dir=cache,
            output_dir=review_path.parent, pins=pins,
        )


def test_independent_freeze_requires_full_pre_exchange_blind_attestation(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    draft = tmp_path / "draft.json"
    create_independent_ledger_template(
        review_manifest_path=review_path, output_path=draft,
        reviewer_id="reviewer-a", pins=pins,
    )
    _complete_independent(draft)
    payload = json.loads(draft.read_text())
    payload["peer_ledger_read"] = True
    draft.write_text(canonical_json(payload) + "\n")
    with pytest.raises(ValueError, match="peer reference must remain unread"):
        freeze_independent_ledger(
            review_manifest_path=review_path, draft_path=draft,
            output_path=tmp_path / "bad.json", pins=pins,
        )


def test_reconciliation_detects_every_material_axis_and_requires_resolution(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    ledger_a, ledger_b = _two_frozen(tmp_path, pins, review_path)
    draft = tmp_path / "reconcile-draft.json"
    template = create_reconciliation_template(
        review_manifest_path=review_path, ledger_a_path=ledger_a, ledger_b_path=ledger_b,
        output_path=draft, reconciler_id="reconciler", pins=pins,
    )
    by_id = {row["inference_id"]: row for row in template["rows"]}
    medium_row = next(row for row in by_id.values() if "primary.medium" in row["disagreement_dimensions"])
    assert {"primary.medium", "acceptable.medium"} <= set(
        medium_row["disagreement_dimensions"]
    )
    resolution_row = next(row for row in by_id.values() if "resolution.resolution_insufficient" in row["disagreement_dimensions"])
    assert set(resolution_row["unresolved_dimensions"]) == {
        "uncertainty.expected_uncertain_axes",
        "resolution.resolution_insufficient",
    }
    template["reconciliation_completed_before_unblinding"] = True
    draft.write_text(canonical_json(template) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="all disagreement dimensions must be resolved"):
        freeze_reconciled_ledger(
            review_manifest_path=review_path, ledger_a_path=ledger_a, ledger_b_path=ledger_b,
            draft_path=draft, output_path=tmp_path / "bad-frozen.json", pins=pins,
        )


def test_two_frozen_ledgers_reconcile_then_inspect_with_unchanged_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path, predicted_uncertain={1, 2, 3, 4, 5})
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    ledger_a, ledger_b = _two_frozen(tmp_path, pins, review_path)
    draft = tmp_path / "reconcile-draft.json"
    create_reconciliation_template(
        review_manifest_path=review_path, ledger_a_path=ledger_a, ledger_b_path=ledger_b,
        output_path=draft, reconciler_id="reconciler", pins=pins,
    )
    _resolve(draft)
    reconciled = tmp_path / "reconciled.json"
    frozen = freeze_reconciled_ledger(
        review_manifest_path=review_path, ledger_a_path=ledger_a, ledger_b_path=ledger_b,
        draft_path=draft, output_path=reconciled,
        completed_at="2026-08-12T00:20:00Z", pins=pins,
    )
    assert frozen["reconciliation"]["unresolved_dimension_count"] == 0
    assert frozen["reconciliation"]["disagreement_dimension_count"] == 12
    validate_reconciled_frozen_ledger(
        review_manifest_path=review_path, ledger_path=reconciled, pins=pins,
    )
    before = _file_sha(db)
    result = inspect_reconciled_ledger(
        semantic_db=db, review_manifest_path=review_path,
        ledger_path=reconciled, pins=pins,
    )
    assert result["verdict"] == "PASS"
    assert result["gates"]["scope_clear_errors_zero"] is True
    assert result["gates"]["medium_clear_errors_zero"] is True
    assert result["gates"]["uncertainty_quality"] is True
    assert result["reference"]["unresolved_disagreement_count"] == 0
    assert _file_sha(db) == before
    assert not list(db.parent.glob("semantic.db-*"))


def test_complete_with_failures_reviews_all_50_and_scores_only_45_results(
    tmp_path: Path,
) -> None:
    failed = {46, 47, 48, 49, 50}
    pins, selection, db, cache = _build_fixture(
        tmp_path,
        predicted_uncertain={1, 2, 3, 4, 5},
        failed=failed,
    )
    review_path, manifest = _render(tmp_path, pins, selection, db, cache)
    assert manifest["input_count"] == 50
    assert manifest["model_result_accounting"]["available_count"] == 45
    assert manifest["model_result_accounting"]["missing_count"] == 5
    assert {
        int(value.rsplit("_", 1)[1])
        for value in manifest["model_result_accounting"]["missing_inference_ids"]
    } == failed
    assert manifest["model_result_accounting"]["terminal_input_status_counts"] == {
        "success": 45,
        "vision_failed": 5,
    }

    ledger_a, ledger_b = _two_frozen(
        tmp_path, pins, review_path, disagree=False
    )
    draft = tmp_path / "reconcile-failures-draft.json"
    create_reconciliation_template(
        review_manifest_path=review_path,
        ledger_a_path=ledger_a,
        ledger_b_path=ledger_b,
        output_path=draft,
        reconciler_id="reconciler",
        pins=pins,
    )
    _resolve(draft)
    reconciled = tmp_path / "reconciled-failures.json"
    freeze_reconciled_ledger(
        review_manifest_path=review_path,
        ledger_a_path=ledger_a,
        ledger_b_path=ledger_b,
        draft_path=draft,
        output_path=reconciled,
        pins=pins,
    )
    result = inspect_reconciled_ledger(
        semantic_db=db,
        review_manifest_path=review_path,
        ledger_path=reconciled,
        pins=pins,
    )
    assert result["verdict"] == "FAIL"
    assert result["gates"]["result_coverage_100_percent"] is False
    assert result["accounting"]["frozen_reference_rows"] == 50
    assert result["accounting"]["matched_result_occurrences"] == 45
    assert result["accounting"]["missing_result_count"] == 5
    assert {
        int(row["inference_id"].rsplit("_", 1)[1])
        for row in result["accounting"]["missing_results"]
    } == failed
    assert {
        row["vision_status"] for row in result["accounting"]["missing_results"]
    } == {"vision_failed"}
    assert result["fields"]["medium"]["support"] == 45
    assert result["fields"]["medium"]["clear_error_count"] == 0
    assert result["nine_field"]["total_decisions"] == 45 * len(QA_FIELDS)


def test_inspector_rejects_independent_ledger_before_any_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    ledger_a, _ = _two_frozen(tmp_path, pins, review_path, disagree=False)
    monkeypatch.setattr(
        inspector,
        "_open_immutable",
        lambda path: (_ for _ in ()).throw(AssertionError("SQLite opened")),
    )
    with pytest.raises(ValueError, match="frozen reconciled reference"):
        inspect_reconciled_ledger(
            semantic_db=db, review_manifest_path=review_path,
            ledger_path=ledger_a, pins=pins,
        )


def test_reconciliation_module_has_no_sqlite_or_network_model_surface() -> None:
    source = Path("tools/reconcile_cross_source_semantic_review_n10_v12.py").read_text()
    assert "import sqlite3" not in source
    assert "requests" not in source
    assert "run_codex" not in source
    assert "semantic_db" not in source


def test_reconciliation_dimension_contract_is_exhaustive() -> None:
    assert MATERIAL_DIMENSIONS == tuple(
        [f"primary.{field}" for field in QA_FIELDS]
        + [f"acceptable.{field}" for field in QA_FIELDS]
        + [
            "uncertainty.expected_uncertain_axes",
            "resolution.resolution_insufficient",
        ]
    )
    assert len(MATERIAL_DIMENSIONS) == 20
