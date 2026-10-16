from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
import shutil

from canonical.cross_source_semantic_vision_n10_v12_sidecar import (
    _schema,
    run_semantic_vision_n10_v12,
    schema_objects_sha256,
    schema_sql_sha256,
)
from canonical.cross_source_semantic_vision_n10_v12_validator import (
    ValidationContract,
    validate_semantic_n10_v12,
)
from canonical.cross_source_semantic_vision_v12 import PROMPT_TEMPLATE_SHA256
from tests.test_cross_source_semantic_vision_n10_v12_sidecar import _args


def _contract(population) -> ValidationContract:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_schema(population))
        objects_sha = schema_objects_sha256(connection)
    finally:
        connection.close()
    return ValidationContract(
        expected_run_id="semn10v12-"
        + hashlib.sha256(
            (
                population.manifest_self_sha256
                + "\0test-model\0cross-source-image-semantics-prompt-v1.2.0"
            ).encode("utf-8")
        ).hexdigest()[:24],
        manifest_domain=population.manifest_domain,
        manifest_byte_sha256=population.manifest_byte_sha256,
        manifest_self_sha256=population.manifest_self_sha256,
        sample_seed=population.sample_seed,
        occurrence_count=population.occurrence_count,
        ordered_building_manifest_sha256=population.ordered_building_manifest_sha256,
        ordered_occurrence_manifest_sha256=population.ordered_occurrence_manifest_sha256,
        ordered_fetch_url_manifest_sha256=population.ordered_fetch_url_manifest_sha256,
        ordered_e1_pixel_manifest_sha256=population.ordered_e1_pixel_manifest_sha256,
        inference_id_prefix=population.inference_id_prefix,
        max_model_call_reservations=population.max_model_call_reservations,
        observed_input_token_stop_threshold=population.observed_input_token_stop_threshold,
        observed_output_token_stop_threshold=population.observed_output_token_stop_threshold,
        observed_total_token_stop_threshold=population.observed_total_token_stop_threshold,
        expected_schema_sql_sha256=schema_sql_sha256(population),
        expected_schema_objects_sha256=objects_sha,
        expected_prior_run_names=population.prior_run_names,
        expected_prior_successful_derivative_count=population.prior_successful_derivative_count,
        expected_model="test-model",
        expected_reasoning="low",
        expected_service_tier="test",
        expected_image_detail="high",
        expected_cli_version="codex-cli 0.146.0",
        expected_prompt_template_sha256=PROMPT_TEMPLATE_SHA256,
    )


def _validated_fixture(tmp_path: Path):
    args = _args(tmp_path)
    cache = tmp_path / "review-cache"
    args["review_cache_dir"] = cache
    result = run_semantic_vision_n10_v12(**args)
    contract = _contract(args["population"])
    validated = validate_semantic_n10_v12(
        result.output_db,
        result.report_path,
        args["manifest_path"],
        args["e2_path"],
        args["e3_path"],
        cache,
        contract=contract,
    )
    return args, cache, result, contract, validated


def test_independent_validator_replays_terminal_artifact(tmp_path: Path) -> None:
    _, _, _, _, result = _validated_fixture(tmp_path)

    assert result.passed is True
    assert result.counts["buildings"] == 10
    assert result.counts["occurrences"] == 10
    assert result.counts["model_call_reservations"] == 2


def test_independent_validator_rejects_tampered_trigger_sql(tmp_path: Path) -> None:
    args, cache, result, contract, _ = _validated_fixture(tmp_path)
    tampered = tmp_path / "tampered.db"
    shutil.copyfile(result.output_db, tampered)
    connection = sqlite3.connect(tampered)
    try:
        connection.executescript(
            "DROP TRIGGER reservations_no_delete;"
            "CREATE TRIGGER reservations_no_delete BEFORE DELETE ON "
            "external_call_reservations BEGIN SELECT 1; END;"
        )
        connection.commit()
    finally:
        connection.close()

    checked = validate_semantic_n10_v12(
        tampered,
        result.report_path,
        args["manifest_path"],
        args["e2_path"],
        args["e3_path"],
        cache,
        contract=contract,
    )

    assert checked.passed is False
    schema_check = next(value for value in checked.checks if value.name == "schema_objects_pin")
    assert schema_check.passed is False


def test_independent_validator_rejects_unapproved_model_envelope(tmp_path: Path) -> None:
    args, cache, result, contract, _ = _validated_fixture(tmp_path)
    tampered = tmp_path / "wrong-model.db"
    shutil.copyfile(result.output_db, tampered)
    connection = sqlite3.connect(tampered)
    try:
        connection.executescript(
            "DROP TRIGGER semantic_runs_provenance_immutable;"
            "DROP TRIGGER semantic_runs_terminal_immutable;"
            "UPDATE semantic_runs SET model='unapproved-model';"
        )
        connection.commit()
    finally:
        connection.close()

    checked = validate_semantic_n10_v12(
        tampered,
        result.report_path,
        args["manifest_path"],
        args["e2_path"],
        args["e3_path"],
        cache,
        contract=contract,
    )

    assert checked.passed is False
    literal_check = next(
        value for value in checked.checks if value.name == "literal_runtime_contract"
    )
    assert literal_check.passed is False
    assert literal_check.detail["model"] == {
        "actual": "unapproved-model",
        "expected": "test-model",
    }


def test_validator_module_does_not_import_runner() -> None:
    source = Path("canonical/cross_source_semantic_vision_n10_v12_validator.py").read_text(
        encoding="utf-8"
    )
    assert "cross_source_semantic_vision_n10_v12_sidecar" not in source
