from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_json
from canonical.cross_source_semantic_vision_n10_v12_sidecar import (
    PopulationContract,
    RUNNER_VERSION as V12_RUNNER_VERSION,
    SCHEMA_VERSION as V12_SCHEMA_VERSION,
    schema_objects_sha256_for_contract,
    schema_sql_sha256,
)
from canonical.cross_source_semantic_vision_v12 import (
    OUTPUT_SCHEMA,
    PROMPT_TEMPLATE_SHA256,
)
from tools.run_cross_source_semantic_vision_n10_v12 import (
    ProductionPins,
    _model_envelope_preflight,
    _production_contracts,
    _resolve_verified_codex,
    _review_cache_preflight,
)
import tools.run_cross_source_semantic_vision_n10_v12 as run_cli
import tools.validate_cross_source_semantic_vision_n10_v12 as validate_cli


def _pins() -> ProductionPins:
    digest = "0" * 64
    population = PopulationContract(
        manifest_domain="test-v12",
        manifest_byte_sha256=digest,
        manifest_self_sha256=digest,
        sample_seed="test-seed",
        occurrence_count=10,
        ordered_building_manifest_sha256=digest,
        ordered_occurrence_manifest_sha256=digest,
        ordered_fetch_url_manifest_sha256=digest,
        ordered_e1_pixel_manifest_sha256=digest,
        inference_id_prefix="semv12_",
        max_model_call_reservations=4,
        observed_input_token_stop_threshold=100,
        observed_output_token_stop_threshold=20,
        observed_total_token_stop_threshold=120,
        prior_run_names=("old-a", "old-b", "old-c"),
        prior_successful_derivative_count=7,
    )
    return ProductionPins(
        run_id="semn10v12-"
        + hashlib.sha256(
            (
                population.manifest_self_sha256
                + "\0gpt-5.6-sol\0cross-source-image-semantics-prompt-v1.2.0"
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
        prior_run_names=population.prior_run_names,
        prior_successful_derivative_count=population.prior_successful_derivative_count,
        max_model_call_reservations=population.max_model_call_reservations,
        observed_input_token_stop_threshold=population.observed_input_token_stop_threshold,
        observed_output_token_stop_threshold=population.observed_output_token_stop_threshold,
        observed_total_token_stop_threshold=population.observed_total_token_stop_threshold,
        model="gpt-5.6-sol",
        reasoning="low",
        service_tier="fast",
        image_detail="high",
        cli_version="codex-cli 0.147.0",
        prompt_template_sha256=PROMPT_TEMPLATE_SHA256,
        output_schema_sha256=hashlib.sha256(
            canonical_json(OUTPUT_SCHEMA).encode("utf-8")
        ).hexdigest(),
        schema_sql_sha256=schema_sql_sha256(population),
        schema_objects_sha256=schema_objects_sha256_for_contract(population),
    )


def test_unpinned_run_and_validator_clis_fail_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_cli, "PRODUCTION_PINS", None)
    monkeypatch.setattr(validate_cli, "PRODUCTION_CONTRACT", None)
    assert run_cli.main(["--repo-root", str(tmp_path)]) == 2
    assert validate_cli.main(["--sidecar", str(tmp_path / "missing.db")]) == 2
    assert list(tmp_path.iterdir()) == []


def test_literal_prompt_runtime_contract_is_exact_when_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_cli, "PRODUCTION_PINS", _pins())
    population, validation = _production_contracts()
    assert population.inference_id_prefix == "semv12_"
    assert population.prior_run_names == ("old-a", "old-b", "old-c")
    assert validation.expected_prior_successful_derivative_count == 7
    assert validation.expected_model == "gpt-5.6-sol"


def test_frozen_production_pins_bind_exact_manifest_and_validator() -> None:
    pins = run_cli.PRODUCTION_PINS
    assert pins is not None
    manifest = Path("data/reports/cross_source_semantic_coverage_n10_v1_2.json")
    raw = manifest.read_bytes()
    assert len(raw) == 983_229
    assert hashlib.sha256(raw).hexdigest() == pins.manifest_byte_sha256
    assert pins.occurrence_count == 50
    assert pins.max_model_call_reservations == 12
    population, validation = _production_contracts()
    assert validation == validate_cli.PRODUCTION_CONTRACT
    run_id = "semn10v12-" + hashlib.sha256(
        (
            population.manifest_self_sha256
            + "\0"
            + pins.model
            + "\0cross-source-image-semantics-prompt-v1.2.0"
        ).encode("utf-8")
    ).hexdigest()[:24]
    assert run_id == "semn10v12-82ddad3e9cb37d3f640209c7"


def test_model_envelope_is_literal_and_fails_closed_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_cli, "PRODUCTION_PINS", _pins())
    _, validation = _production_contracts()
    _model_envelope_preflight("gpt-5.6-sol", "low", "fast", validation)
    with pytest.raises(RuntimeError, match="model envelope mismatch"):
        _model_envelope_preflight("other-model", "low", "fast", validation)
    assert run_cli.main(
        ["--model", "other-model", "--repo-root", str(tmp_path)]
    ) == 2
    assert list(tmp_path.iterdir()) == []


def test_fresh_and_resume_review_cache_no_clobber(tmp_path: Path) -> None:
    cache = tmp_path / "review-cache"
    cache.mkdir()
    with pytest.raises(FileExistsError, match="refuses existing review cache"):
        _review_cache_preflight(cache, resume=False)
    _review_cache_preflight(cache, resume=True)
    with pytest.raises(FileNotFoundError, match="requires its existing review cache"):
        _review_cache_preflight(tmp_path / "missing", resume=True)


def test_run_cli_wires_mandatory_preflight_and_terminal_validator() -> None:
    source = Path("tools/run_cross_source_semantic_vision_n10_v12.py").read_text(
        encoding="utf-8"
    )
    assert "preflight_validator=mandatory_preflight" in source
    assert "validate_semantic_n10_v12(" in source
    assert "review_cache_dir=args.review_cache_dir" in source
    assert "cli_version=actual_cli_version" in source


def test_codex_discovery_and_explicit_version_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"test executable placeholder")
    monkeypatch.setattr(run_cli, "discover_codex_bin", lambda: binary)
    monkeypatch.setattr(run_cli, "codex_version", lambda path: "codex-cli 0.146.0")
    assert _resolve_verified_codex(None) == (binary.resolve(), "codex-cli 0.146.0")
    monkeypatch.setattr(run_cli, "codex_version", lambda path: "codex-cli 0.145.9")
    with pytest.raises(RuntimeError, match="too old"):
        _resolve_verified_codex(binary)


def test_explicit_codex_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _resolve_verified_codex(tmp_path / "missing-codex.exe")


def test_main_rejects_supported_but_unapproved_cli_before_manifest_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_cli, "PRODUCTION_PINS", _pins())
    monkeypatch.setattr(
        run_cli,
        "_resolve_verified_codex",
        lambda explicit: (tmp_path / "codex.exe", "codex-cli 0.146.0"),
    )
    monkeypatch.setattr(
        run_cli,
        "_manifest_preflight",
        lambda *args, **kwargs: pytest.fail("manifest preflight must not run"),
    )
    assert run_cli.main(
        ["--repo-root", str(tmp_path), "--review-cache-dir", str(tmp_path / "cache")]
    ) == 2
    assert list(tmp_path.iterdir()) == []


def test_v12_release_surface_is_separate_from_frozen_v11() -> None:
    from canonical.cross_source_semantic_vision_n10_v11_sidecar import (
        RUNNER_VERSION as V11_RUNNER_VERSION,
        SCHEMA_VERSION as V11_SCHEMA_VERSION,
    )

    for path in (
        Path("canonical/cross_source_semantic_vision_n10_v12_sidecar.py"),
        Path("canonical/cross_source_semantic_vision_n10_v12_validator.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "cross_source_semantic_vision_n10_v11" not in source
    assert V12_SCHEMA_VERSION == 4
    assert V11_SCHEMA_VERSION == 3
    assert V12_RUNNER_VERSION != V11_RUNNER_VERSION
