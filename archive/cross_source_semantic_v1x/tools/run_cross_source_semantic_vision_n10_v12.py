#!/usr/bin/env python
"""Run or exactly resume the approved semantic Vision N10 v1.2.

The independent v1.2 planner manifest and approved execution envelope are
copied below as production literals.  Any mismatch fails before executable
discovery, filesystem creation, HTTP, or model execution.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_image_selection import canonical_json  # noqa: E402
from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E3_RELATIVE_PATH,
    default_specs,
)
from canonical.cross_source_semantic_vision_n10_v12_sidecar import (  # noqa: E402
    PopulationContract,
    SemanticContract,
    run_semantic_vision_n10_v12,
    schema_objects_sha256_for_contract,
    schema_sql_sha256,
)
from canonical.cross_source_semantic_vision_n10_v12_validator import (  # noqa: E402
    ValidationContract,
    validate_semantic_n10_v12,
)
from canonical.cross_source_semantic_vision_sidecar import COVERAGE_SLOTS  # noqa: E402
from canonical.cross_source_semantic_vision_v12 import (  # noqa: E402
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_TEMPLATE_SHA256,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_batch,
)
from canonical.divisare_vision_runtime import (  # noqa: E402
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
)
from tools.run_divisare_vision_benchmark import (  # noqa: E402
    codex_version,
    discover_codex_bin,
    require_supported_cli,
)


CLI_VERSION = "cross-source-semantic-vision-n10-v1.2-cli-v1.0.0"
DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_2.json")
DEFAULT_OUTPUT = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db")
DEFAULT_REPORT = Path("data/reports/divisare_architizer_semantic_vision_n10_v1_2.md")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_cache")


@dataclass(frozen=True)
class ProductionPins:
    run_id: str
    manifest_domain: str
    manifest_byte_sha256: str
    manifest_self_sha256: str
    sample_seed: str
    occurrence_count: int
    ordered_building_manifest_sha256: str
    ordered_occurrence_manifest_sha256: str
    ordered_fetch_url_manifest_sha256: str
    ordered_e1_pixel_manifest_sha256: str
    prior_run_names: tuple[str, ...]
    prior_successful_derivative_count: int
    max_model_call_reservations: int
    observed_input_token_stop_threshold: int
    observed_output_token_stop_threshold: int
    observed_total_token_stop_threshold: int
    model: str
    reasoning: str
    service_tier: str
    image_detail: str
    cli_version: str
    prompt_template_sha256: str
    output_schema_sha256: str
    schema_sql_sha256: str
    schema_objects_sha256: str


PRODUCTION_PINS: ProductionPins | None = ProductionPins(
    run_id="semn10v12-82ddad3e9cb37d3f640209c7",
    manifest_domain="archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1",
    manifest_byte_sha256="00d494c58590a9a0ca3d78de858f857f6a1e226390018f5426d799a846ead8fe",
    manifest_self_sha256="44f9c5ca47adbacd8fc0f87be11e59b6468d57205831e2c5d78b0f96438a495a",
    sample_seed="archibe-semantic-coverage-n10-v1.2",
    occurrence_count=50,
    ordered_building_manifest_sha256="8008b9972ce779488e280a0709013a3a13916d9936ce1aaafb1fa1a13c3363fb",
    ordered_occurrence_manifest_sha256="939cea9f514a0caeb4fa4ec7bc9688f062ae212318f4d458ae551b04574cc314",
    ordered_fetch_url_manifest_sha256="1cf542b62edd93defd375d1e070f06975bae92ce4b5d80a2834a63d77e990d3e",
    ordered_e1_pixel_manifest_sha256="613bce3f755497e9944c7368c6d720bd913a7ee58d1523afef2bef09d3e05d5c",
    prior_run_names=(
        "semantic_n10_v1",
        "semantic_n100_v1",
        "semantic_n10_v1_1",
    ),
    prior_successful_derivative_count=665,
    max_model_call_reservations=12,
    observed_input_token_stop_threshold=250_000,
    observed_output_token_stop_threshold=10_000,
    observed_total_token_stop_threshold=260_000,
    model="gpt-5.6-sol",
    reasoning="low",
    service_tier="fast",
    image_detail="high",
    cli_version="codex-cli 0.147.0",
    prompt_template_sha256="041d6b7b0c6de4abc9989c585cd7a45267932eb6812f0225e0d12f11b99f7d73",
    output_schema_sha256="e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4",
    schema_sql_sha256="7eebb5135000125d3e2a453bf48526d28518fe78fe2d861672b18b30bd382640",
    schema_objects_sha256="e77a4be8896aa474720155e217162a7592d0f5a6018c3b8c2c5ea1f09efc2401",
)


SEMANTICS = SemanticContract(
    contract_version=CONTRACT_VERSION,
    prompt_version=PROMPT_VERSION,
    transform_version=TRANSFORM_VERSION,
    output_schema=OUTPUT_SCHEMA,
    compose_prompt=compose_prompt,
    normalize_batch=normalize_batch,
    derive_hero_decision=derive_hero_decision,
    derive_coverage_slots=derive_coverage_slots,
    coverage_slots=COVERAGE_SLOTS,
)


def _production_contracts() -> tuple[PopulationContract, ValidationContract]:
    pins = PRODUCTION_PINS
    if pins is None:
        raise RuntimeError("v1.2 production manifest pins are not finalized")
    population = PopulationContract(
        manifest_domain=pins.manifest_domain,
        manifest_byte_sha256=pins.manifest_byte_sha256,
        manifest_self_sha256=pins.manifest_self_sha256,
        sample_seed=pins.sample_seed,
        occurrence_count=pins.occurrence_count,
        ordered_building_manifest_sha256=pins.ordered_building_manifest_sha256,
        ordered_occurrence_manifest_sha256=pins.ordered_occurrence_manifest_sha256,
        ordered_fetch_url_manifest_sha256=pins.ordered_fetch_url_manifest_sha256,
        ordered_e1_pixel_manifest_sha256=pins.ordered_e1_pixel_manifest_sha256,
        inference_id_prefix="semv12_",
        max_model_call_reservations=pins.max_model_call_reservations,
        observed_input_token_stop_threshold=pins.observed_input_token_stop_threshold,
        observed_output_token_stop_threshold=pins.observed_output_token_stop_threshold,
        observed_total_token_stop_threshold=pins.observed_total_token_stop_threshold,
        prior_run_names=pins.prior_run_names,
        prior_successful_derivative_count=pins.prior_successful_derivative_count,
    )
    actual = {
        "run_id": "semn10v12-"
        + hashlib.sha256(
            (
                pins.manifest_self_sha256
                + "\0"
                + pins.model
                + "\0"
                + PROMPT_VERSION
            ).encode("utf-8")
        ).hexdigest()[:24],
        "prompt_template_sha256": PROMPT_TEMPLATE_SHA256,
        "output_schema_sha256": hashlib.sha256(
            canonical_json(OUTPUT_SCHEMA).encode("utf-8")
        ).hexdigest(),
        "schema_sql_sha256": schema_sql_sha256(population),
        "schema_objects_sha256": schema_objects_sha256_for_contract(population),
    }
    expected = {
        "run_id": pins.run_id,
        "prompt_template_sha256": pins.prompt_template_sha256,
        "output_schema_sha256": pins.output_schema_sha256,
        "schema_sql_sha256": pins.schema_sql_sha256,
        "schema_objects_sha256": pins.schema_objects_sha256,
    }
    if actual != expected:
        raise RuntimeError(
            "v1.2 literal prompt/runtime contract changed: "
            + canonical_json({"actual": actual, "expected": expected})
        )
    validation = ValidationContract(
        expected_run_id=pins.run_id,
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
        expected_schema_sql_sha256=pins.schema_sql_sha256,
        expected_schema_objects_sha256=pins.schema_objects_sha256,
        expected_prior_run_names=pins.prior_run_names,
        expected_prior_successful_derivative_count=pins.prior_successful_derivative_count,
        expected_model=pins.model,
        expected_reasoning=pins.reasoning,
        expected_service_tier=pins.service_tier,
        expected_image_detail=pins.image_detail,
        expected_cli_version=pins.cli_version,
        expected_prompt_template_sha256=pins.prompt_template_sha256,
    )
    return population, validation


def _model_envelope_preflight(
    model: str,
    reasoning: str,
    service_tier: str,
    contract: ValidationContract,
) -> None:
    actual = {
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "image_detail": CLI_IMAGE_DETAIL,
    }
    expected = {
        "model": contract.expected_model,
        "reasoning": contract.expected_reasoning,
        "service_tier": contract.expected_service_tier,
        "image_detail": contract.expected_image_detail,
    }
    if actual != expected:
        raise RuntimeError(
            "v1.2 production model envelope mismatch: "
            + canonical_json({"actual": actual, "expected": expected})
        )


def _manifest_preflight(path: Path, repo_root: Path):
    from canonical.cross_source_semantic_coverage_n10_v12_validator import (
        default_prior_specs,
        validate_manifest,
    )

    e2_spec, e3_spec = default_specs(repo_root)
    result = validate_manifest(
        path,
        e2_spec=e2_spec,
        e3_spec=e3_spec,
        prior_specs=default_prior_specs(repo_root),
        expected_seed="archibe-semantic-coverage-n10-v1.2",
    )
    if not result.passed:
        raise RuntimeError(
            "mandatory independent coverage preflight failed: "
            + canonical_json(result.as_record())
        )
    return result


def _review_cache_preflight(path: Path, *, resume: bool) -> None:
    if resume:
        if not path.is_dir():
            raise FileNotFoundError(f"resume requires its existing review cache: {path}")
        return
    if path.exists():
        raise FileExistsError(f"fresh v1.2 run refuses existing review cache: {path}")


def _resolve_verified_codex(explicit: Path | None) -> tuple[Path, str]:
    path = explicit.expanduser().resolve() if explicit is not None else discover_codex_bin().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Codex CLI does not exist: {path}")
    version = codex_version(path)
    require_supported_cli(version)
    return path, version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the byte-pinned semantic N10 v1.2.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--e2", type=Path, default=DEFAULT_E2_RELATIVE_PATH)
    parser.add_argument("--e3", type=Path, default=DEFAULT_E3_RELATIVE_PATH)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE)
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        population, validation_contract = _production_contracts()
        _model_envelope_preflight(
            args.model, args.reasoning, args.service_tier, validation_contract
        )
        _review_cache_preflight(args.review_cache_dir.resolve(), resume=args.resume)
        codex_bin, actual_cli_version = _resolve_verified_codex(args.codex_bin)
        if actual_cli_version != validation_contract.expected_cli_version:
            raise RuntimeError(
                "v1.2 production Codex CLI version mismatch: "
                + canonical_json(
                    {
                        "actual": actual_cli_version,
                        "expected": validation_contract.expected_cli_version,
                    }
                )
            )
        preflight = _manifest_preflight(args.manifest.resolve(), args.repo_root.resolve())

        def mandatory_preflight(path: Path):
            return _manifest_preflight(path, args.repo_root.resolve())

        result = run_semantic_vision_n10_v12(
            population=population,
            semantics=SEMANTICS,
            manifest_path=args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
            output_db=args.output_db,
            report_path=args.report,
            codex_bin=codex_bin,
            model=args.model,
            reasoning=args.reasoning,
            service_tier=args.service_tier,
            cli_version=actual_cli_version,
            resume=args.resume,
            review_cache_dir=args.review_cache_dir,
            preflight_validator=mandatory_preflight,
        )
        validation = validate_semantic_n10_v12(
            args.output_db,
            args.report,
            args.manifest,
            args.e2,
            args.e3,
            args.review_cache_dir,
            contract=validation_contract,
        )
        if not validation.passed:
            raise RuntimeError(
                "mandatory independent terminal validation failed: "
                + canonical_json(validation.as_dict())
            )
    except Exception as exc:
        print(
            json.dumps(
                {"error": str(exc), "error_type": type(exc).__name__, "status": "error"},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "already_complete": result.already_complete,
                "codex_bin": str(codex_bin),
                "codex_cli_version": actual_cli_version,
                "coverage_preflight_passed": preflight.passed,
                "independent_terminal_validation_passed": validation.passed,
                "logical_sha256": result.logical_sha256,
                "network_requests": result.physical_http_reservations,
                "output_db": str(result.output_db),
                "report": str(result.report_path),
                "resumed": result.resumed,
                "runner_cli_version": CLI_VERSION,
                "status": result.status,
                "vision_requests": result.model_call_reservations,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
