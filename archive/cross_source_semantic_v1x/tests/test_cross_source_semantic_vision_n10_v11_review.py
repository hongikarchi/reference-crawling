from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_vision_v11 import derive_hero_decision
from tools import inspect_cross_source_semantic_vision_n10_v11 as inspector
from tools.inspect_cross_source_semantic_vision_n10_v11 import (
    QA_FIELDS,
    create_ledger_template,
    freeze_ledger,
    inspect_frozen_ledger,
)
from tools.render_cross_source_semantic_review_sheets_n10_v11 import (
    SELECTION_MANIFEST_DOMAIN,
    ReviewPins,
    _file_sha,
    render_n10_v11_review_sheets,
)
from tools.render_cross_source_semantic_review_sheets import _pixel_identity


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
        "evidence": "A coherent finished exterior is clearly visible.",
    }


def _rejected(inference_id: str) -> dict[str, object]:
    return {
        "asset_id": inference_id,
        "in_scope": False,
        "reject_reason": "isolated_product_artwork_or_sample",
        "medium": "photograph",
        "spatial_context": "not_applicable",
        "framing_scale": "not_applicable",
        "camera_angle": "not_applicable",
        "drawing_kind": "not_applicable",
        "project_state": "not_applicable",
        "project_legibility": "none",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "An isolated product dominates without a coherent project.",
    }


def _pins_and_selection(tmp_path: Path) -> tuple[ReviewPins, Path]:
    body: dict[str, object] = {
        "ordered_building_manifest_sha256": _sha("ordered-buildings"),
        "ordered_occurrence_manifest_sha256": _sha("ordered-occurrences"),
        "ordered_fetch_url_manifest_sha256": _sha("ordered-urls"),
        "ordered_e1_pixel_manifest_sha256": _sha("ordered-pixels"),
        "planned_occurrence_count": 53,
        "sample_size_buildings": 10,
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SELECTION_MANIFEST_DOMAIN, "manifest": body}
    )
    raw = (canonical_json(body) + "\n").encode()
    path = tmp_path / "selection.json"
    path.write_bytes(raw)
    pins = ReviewPins(
        selection_manifest_byte_sha256=hashlib.sha256(raw).hexdigest(),
        selection_manifest_self_sha256=str(body["semantic_coverage_manifest_sha256"]),
        ordered_building_manifest_sha256=str(body["ordered_building_manifest_sha256"]),
        ordered_occurrence_manifest_sha256=str(body["ordered_occurrence_manifest_sha256"]),
        ordered_fetch_url_manifest_sha256=str(body["ordered_fetch_url_manifest_sha256"]),
        ordered_e1_pixel_manifest_sha256=str(body["ordered_e1_pixel_manifest_sha256"]),
        run_id="semn10v11-test-review",
        runner_version="test-v11-runner",
        contract_version="cross-source-image-semantics-v1.1.0",
        prompt_version="cross-source-image-semantics-prompt-v1.1.0",
        output_schema_sha256=_sha("output-schema"),
        schema_sql_sha256=_sha("schema-sql"),
        schema_objects_sha256=_sha("schema-objects"),
        transform_version="cross-source-vision-input-1024-jpeg-v1.0.0",
        model="test-model",
        reasoning="low",
        service_tier="test",
    )
    return pins, path


def _build_fixture(
    tmp_path: Path,
    *,
    predicted_uncertain: set[int] | None = None,
) -> tuple[ReviewPins, Path, Path, Path]:
    predicted_uncertain = predicted_uncertain or set()
    pins, selection = _pins_and_selection(tmp_path)
    cache = tmp_path / "review-cache"
    cache.mkdir()
    images: dict[str, tuple[str, str, int, int, int]] = {}
    for index in range(1, 54):
        inference_id = f"semv11_{index:06d}"
        path = cache / f"{inference_id}.jpg"
        Image.new(
            "RGB",
            (24, 18),
            ((index * 17) % 251, (index * 29) % 251, (index * 41) % 251),
        ).save(path, format="JPEG", quality=90)
        pixel_sha, width, height, image_format = _pixel_identity(path)
        assert image_format == "JPEG"
        images[inference_id] = (
            _file_sha(path),
            pixel_sha,
            width,
            height,
            path.stat().st_size,
        )

    db = tmp_path / "semantic.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE semantic_runs(
          run_id TEXT,runner_version TEXT,contract_version TEXT,prompt_version TEXT,
          output_schema_sha256 TEXT,schema_sql_sha256 TEXT,schema_objects_sha256 TEXT,
          transform_version TEXT,model TEXT,reasoning TEXT,service_tier TEXT,
          manifest_byte_sha256 TEXT,manifest_self_sha256 TEXT,
          ordered_building_manifest_sha256 TEXT,ordered_occurrence_manifest_sha256 TEXT,
          ordered_fetch_url_manifest_sha256 TEXT,ordered_e1_pixel_manifest_sha256 TEXT,
          building_count INTEGER,occurrence_count INTEGER,status TEXT,
          metrics_json TEXT,logical_sha256 TEXT
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
        "INSERT INTO semantic_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pins.run_id,
            pins.runner_version,
            pins.contract_version,
            pins.prompt_version,
            pins.output_schema_sha256,
            pins.schema_sql_sha256,
            pins.schema_objects_sha256,
            pins.transform_version,
            pins.model,
            pins.reasoning,
            pins.service_tier,
            pins.selection_manifest_byte_sha256,
            pins.selection_manifest_self_sha256,
            pins.ordered_building_manifest_sha256,
            pins.ordered_occurrence_manifest_sha256,
            pins.ordered_fetch_url_manifest_sha256,
            pins.ordered_e1_pixel_manifest_sha256,
            10,
            53,
            "complete",
            canonical_json(metrics),
            _sha("logical-db"),
        ),
    )
    connection.executemany(
        "INSERT INTO selected_buildings VALUES(?,?)",
        [(pins.run_id, f"building-{index}") for index in range(1, 11)],
    )
    for index, (inference_id, image) in enumerate(images.items(), 1):
        semantic = _normal(inference_id, uncertain=index in predicted_uncertain)
        connection.execute(
            "INSERT INTO selected_occurrences VALUES(?,?,?)",
            (pins.run_id, index, inference_id),
        )
        connection.execute(
            "INSERT INTO vision_inputs VALUES(?,?,?,?,?,?,?,?)",
            (pins.run_id, inference_id, "success", *image),
        )
        connection.execute(
            "INSERT INTO semantic_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pins.run_id,
                inference_id,
                int(bool(semantic["in_scope"])),
                semantic["reject_reason"],
                semantic["medium"],
                semantic["spatial_context"],
                semantic["framing_scale"],
                semantic["camera_angle"],
                semantic["drawing_kind"],
                semantic["project_state"],
                semantic["project_legibility"],
                canonical_json(semantic["uncertain_axes"]),
                int(bool(semantic["resolution_insufficient"])),
                semantic["evidence"],
            ),
        )
        connection.execute(
            "INSERT INTO occurrence_result_links VALUES(?,?,?)",
            (pins.run_id, inference_id, inference_id),
        )
        connection.execute(
            "INSERT INTO hero_candidate_decisions VALUES(?,?,?)",
            (pins.run_id, inference_id, derive_hero_decision(semantic)[0]),
        )
    connection.commit()
    connection.close()
    return pins, selection, db, cache


def _render(tmp_path: Path, pins: ReviewPins, selection: Path, db: Path, cache: Path):
    output = tmp_path / "review-sheets"
    manifest = render_n10_v11_review_sheets(
        semantic_db=db,
        selection_manifest=selection,
        review_cache_dir=cache,
        output_dir=output,
        pins=pins,
    )
    return output / "manifest.json", manifest


def _complete_draft(
    draft_path: Path,
    *,
    uncertain: set[int] | None = None,
    rejected: set[int] | None = None,
) -> None:
    uncertain = uncertain or set()
    rejected = rejected or set()
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    payload["blind_review_completed_before_unblinding"] = True
    for row in payload["rows"]:
        source_index = int(str(row["inference_id"]).rsplit("_", 1)[1])
        semantic = _rejected(row["inference_id"]) if source_index in rejected else _normal(
            row["inference_id"], uncertain=source_index in uncertain
        )
        row["primary"] = {field: semantic[field] for field in QA_FIELDS}
        row["acceptable_values"] = {
            field: [semantic[field]] for field in QA_FIELDS
        }
        row["expected_uncertain_axes"] = list(semantic["uncertain_axes"])
        row["resolution_insufficient"] = semantic["resolution_insufficient"]
        row["reason"] = semantic["evidence"]
    draft_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def test_renderer_binds_exact_53_v11_inputs_and_is_no_clobber(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, manifest = _render(tmp_path, pins, selection, db, cache)

    assert manifest["input_count"] == 53
    assert len(manifest["ordered_inference_ids"]) == 53
    assert all(value.startswith("semv11_") for value in manifest["ordered_inference_ids"])
    assert manifest["semantic_run"]["run_id"] == pins.run_id
    assert manifest["selection_manifest"]["byte_sha256"] == pins.selection_manifest_byte_sha256
    assert manifest["semantic_run"]["review_cache_provenance"]["retained_for_review"] is True
    assert review_path.is_file()

    with pytest.raises(FileExistsError):
        render_n10_v11_review_sheets(
            semantic_db=db,
            selection_manifest=selection,
            review_cache_dir=cache,
            output_dir=review_path.parent,
            pins=pins,
        )


def test_template_and_freeze_never_open_db_then_all_gates_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins, selection, db, cache = _build_fixture(
        tmp_path, predicted_uncertain={1, 2, 3, 4, 5}
    )
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    draft = tmp_path / "draft.json"
    frozen = tmp_path / "frozen.json"

    monkeypatch.setattr(
        inspector,
        "_open_immutable",
        lambda path: (_ for _ in ()).throw(AssertionError("SQLite opened before freeze")),
    )
    create_ledger_template(
        review_manifest_path=review_path,
        output_path=draft,
        reviewer_id="blind-agent",
        created_at="2026-08-12T00:00:00Z",
        pins=pins,
    )
    _complete_draft(draft, uncertain={1, 2, 3, 4, 5})
    freeze_ledger(
        review_manifest_path=review_path,
        draft_path=draft,
        output_path=frozen,
        completed_at="2026-08-12T00:10:00Z",
        pins=pins,
    )

    monkeypatch.setattr(inspector, "_open_immutable", __import__(
        "tools.render_cross_source_semantic_review_sheets_n10_v11",
        fromlist=["_open_immutable"],
    )._open_immutable)
    before = _file_sha(db)
    result = inspect_frozen_ledger(
        semantic_db=db,
        review_manifest_path=review_path,
        ledger_path=frozen,
        pins=pins,
    )
    assert result["verdict"] == "PASS", (
        result["gates"], result["hero"], result["uncertainty"]
    )
    assert result["accounting"] == {
        "buildings": 10,
        "frozen_reference_rows": 53,
        "occurrences": 53,
        "semantic_results": 53,
        "successful_inputs": 53,
    }
    assert result["gates"]["scope_clear_errors_zero"] is True
    assert result["gates"]["medium_clear_errors_zero"] is True
    assert result["gates"]["uncertainty_quality"] is True
    assert result["uncertainty"]["rows"]["precision"] == 1.0
    assert result["uncertainty"]["rows"]["recall"] == 1.0
    assert _file_sha(db) == before
    assert not list(db.parent.glob("semantic.db-*"))


def test_uncertainty_support_below_five_is_inconclusive(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    draft = tmp_path / "draft.json"
    frozen = tmp_path / "frozen.json"
    create_ledger_template(
        review_manifest_path=review_path,
        output_path=draft,
        reviewer_id="blind-agent",
        pins=pins,
    )
    _complete_draft(draft)
    freeze_ledger(
        review_manifest_path=review_path,
        draft_path=draft,
        output_path=frozen,
        pins=pins,
    )
    result = inspect_frozen_ledger(
        semantic_db=db,
        review_manifest_path=review_path,
        ledger_path=frozen,
        pins=pins,
    )
    assert result["verdict"] == "PASS_WITH_UNCERTAINTY_INCONCLUSIVE"
    assert result["gates"]["uncertainty_quality"] is None
    assert result["gates"]["uncertainty_quality_status"] == "INCONCLUSIVE_REFERENCE_SUPPORT_LT_5"


def test_critical_hero_false_positive_and_scope_error_fail(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    review_path, _ = _render(tmp_path, pins, selection, db, cache)
    draft = tmp_path / "draft.json"
    frozen = tmp_path / "frozen.json"
    create_ledger_template(
        review_manifest_path=review_path,
        output_path=draft,
        reviewer_id="blind-agent",
        pins=pins,
    )
    _complete_draft(draft, rejected={1})
    freeze_ledger(
        review_manifest_path=review_path,
        draft_path=draft,
        output_path=frozen,
        pins=pins,
    )
    result = inspect_frozen_ledger(
        semantic_db=db,
        review_manifest_path=review_path,
        ledger_path=frozen,
        pins=pins,
    )
    assert result["verdict"] == "FAIL"
    assert result["gates"]["scope_clear_errors_zero"] is False
    assert result["gates"]["critical_hero_false_positives_zero"] is False
    assert result["hero"]["critical_false_positive_rows"][0]["inference_id"].startswith("semv11_")


def test_renderer_rejects_selection_byte_tamper_and_sqlite_sidecar(tmp_path: Path) -> None:
    pins, selection, db, cache = _build_fixture(tmp_path)
    original = selection.read_bytes()
    selection.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="selection manifest byte SHA mismatch"):
        _render(tmp_path, pins, selection, db, cache)

    selection.write_bytes(original)
    sidecar = Path(str(db) + "-wal")
    sidecar.write_bytes(b"not-a-real-wal")
    with pytest.raises(ValueError, match="SQLite sidecars"):
        render_n10_v11_review_sheets(
            semantic_db=db,
            selection_manifest=tmp_path / "selection.json",
            review_cache_dir=cache,
            output_dir=tmp_path / "sidecar-review",
            pins=pins,
        )
