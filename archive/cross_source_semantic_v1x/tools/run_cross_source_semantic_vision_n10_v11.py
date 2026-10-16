#!/usr/bin/env python
"""Run or exactly resume the approved disjoint semantic Vision N10 v1.1."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_image_selection import canonical_json  # noqa: E402
from canonical.cross_source_semantic_coverage_n10_v11 import MANIFEST_DOMAIN  # noqa: E402
from canonical.cross_source_semantic_coverage_n10_v11_validator import (  # noqa: E402
    default_prior_specs,
    validate_manifest,
)
from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E3_RELATIVE_PATH,
    default_specs,
)
from canonical.cross_source_semantic_vision_n10_v11_sidecar import (  # noqa: E402
    PopulationContract,
    SemanticContract,
    run_semantic_vision_n10_v11,
    schema_objects_sha256_for_contract,
    schema_sql_sha256,
)
from canonical.cross_source_semantic_vision_n10_v11_validator import (  # noqa: E402
    ValidationContract,
    validate_semantic_n10_v11,
)
from canonical.cross_source_semantic_vision_sidecar import COVERAGE_SLOTS  # noqa: E402
from canonical.cross_source_semantic_vision_v11 import (  # noqa: E402
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
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
)
from tools.run_divisare_vision_benchmark import (  # noqa: E402
    codex_version,
    discover_codex_bin,
    require_supported_cli,
)


CLI_VERSION = "cross-source-semantic-vision-n10-v1.1-cli-v1.0.0"
DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_1.json")
DEFAULT_OUTPUT = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db")
DEFAULT_REPORT = Path("data/reports/divisare_architizer_semantic_vision_n10_v1_1.md")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_cache")

CANONICAL_MANIFEST_BYTE_SHA256 = "3315a48188aec4c9461867235999ac7e526b8b011a869cafdd3be9d3cd1cc5b6"
CANONICAL_MANIFEST_SELF_SHA256 = "91957a822915ae70f56fd1a5f82244098a4508274cc5dbbdd91e1d6bf03ad8c9"
CANONICAL_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1.1"
CANONICAL_OCCURRENCE_COUNT = 53
CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256 = "7b4c9558682f231e4fce29d0e915666fe1968707e1fea177d58ac0ad514af55d"
CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256 = "0be0449356388c117e59856caacdf14900b694b0c1ee811f807b956c9449fcb6"
CANONICAL_ORDERED_FETCH_URL_MANIFEST_SHA256 = "9de60a38b4ef5e05744c0761c13e84d0a33fe88df6e09ea4b8c9ea24ac527c43"
CANONICAL_ORDERED_E1_PIXEL_MANIFEST_SHA256 = "0ec3a756a3a55480b077e6eb114f4b2df9d1497f0b4dd015e53008e1783324a8"
CANONICAL_PROMPT_TEMPLATE_SHA256 = "42b43a9168ea3315809182da5ff6a5ecdf280cbaabbfa2a369a88806cc7d50ba"
CANONICAL_OUTPUT_SCHEMA_SHA256 = "e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4"
CANONICAL_SCHEMA_SQL_SHA256 = "85b12261ce7c720c21bbad74669c7bc89b0c7e185ba259ff9a552e15608df31e"
CANONICAL_SCHEMA_OBJECTS_SHA256 = "959f95fe3a3ae9f6daa6ee9978eedc9abc00a5bad6c7fe6ef577ac03c02c3fe1"
MAX_MODEL_CALL_RESERVATIONS = 13
OBSERVED_INPUT_TOKEN_STOP_THRESHOLD = 260_000
OBSERVED_OUTPUT_TOKEN_STOP_THRESHOLD = 10_000
OBSERVED_TOTAL_TOKEN_STOP_THRESHOLD = 270_000


POPULATION = PopulationContract(
    manifest_domain=MANIFEST_DOMAIN,
    manifest_byte_sha256=CANONICAL_MANIFEST_BYTE_SHA256,
    manifest_self_sha256=CANONICAL_MANIFEST_SELF_SHA256,
    sample_seed=CANONICAL_SAMPLE_SEED,
    occurrence_count=CANONICAL_OCCURRENCE_COUNT,
    ordered_building_manifest_sha256=CANONICAL_ORDERED_BUILDING_MANIFEST_SHA256,
    ordered_occurrence_manifest_sha256=CANONICAL_ORDERED_OCCURRENCE_MANIFEST_SHA256,
    ordered_fetch_url_manifest_sha256=CANONICAL_ORDERED_FETCH_URL_MANIFEST_SHA256,
    ordered_e1_pixel_manifest_sha256=CANONICAL_ORDERED_E1_PIXEL_MANIFEST_SHA256,
    inference_id_prefix="semv11_",
    max_model_call_reservations=MAX_MODEL_CALL_RESERVATIONS,
    observed_input_token_stop_threshold=OBSERVED_INPUT_TOKEN_STOP_THRESHOLD,
    observed_output_token_stop_threshold=OBSERVED_OUTPUT_TOKEN_STOP_THRESHOLD,
    observed_total_token_stop_threshold=OBSERVED_TOTAL_TOKEN_STOP_THRESHOLD,
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
VALIDATION = ValidationContract(
    manifest_domain=POPULATION.manifest_domain,
    manifest_byte_sha256=POPULATION.manifest_byte_sha256,
    manifest_self_sha256=POPULATION.manifest_self_sha256,
    sample_seed=POPULATION.sample_seed,
    occurrence_count=POPULATION.occurrence_count,
    ordered_building_manifest_sha256=POPULATION.ordered_building_manifest_sha256,
    ordered_occurrence_manifest_sha256=POPULATION.ordered_occurrence_manifest_sha256,
    ordered_fetch_url_manifest_sha256=POPULATION.ordered_fetch_url_manifest_sha256,
    ordered_e1_pixel_manifest_sha256=POPULATION.ordered_e1_pixel_manifest_sha256,
    inference_id_prefix=POPULATION.inference_id_prefix,
    max_model_call_reservations=POPULATION.max_model_call_reservations,
    observed_input_token_stop_threshold=POPULATION.observed_input_token_stop_threshold,
    observed_output_token_stop_threshold=POPULATION.observed_output_token_stop_threshold,
    observed_total_token_stop_threshold=POPULATION.observed_total_token_stop_threshold,
    expected_schema_sql_sha256=CANONICAL_SCHEMA_SQL_SHA256,
    expected_schema_objects_sha256=CANONICAL_SCHEMA_OBJECTS_SHA256,
    expected_prompt_template_sha256=CANONICAL_PROMPT_TEMPLATE_SHA256,
)


def _assert_literal_contract() -> None:
    actual = {
        "contract": SEMANTICS.contract_version,
        "prompt": SEMANTICS.prompt_version,
        "prompt_template_sha256": PROMPT_TEMPLATE_SHA256,
        "output_schema_sha256": hashlib.sha256(canonical_json(OUTPUT_SCHEMA).encode("utf-8")).hexdigest(),
        "schema_sql_sha256": schema_sql_sha256(POPULATION),
        "schema_objects_sha256": schema_objects_sha256_for_contract(POPULATION),
    }
    expected = {
        "contract": "cross-source-image-semantics-v1.1.0",
        "prompt": "cross-source-image-semantics-prompt-v1.1.0",
        "prompt_template_sha256": CANONICAL_PROMPT_TEMPLATE_SHA256,
        "output_schema_sha256": CANONICAL_OUTPUT_SCHEMA_SHA256,
        "schema_sql_sha256": CANONICAL_SCHEMA_SQL_SHA256,
        "schema_objects_sha256": CANONICAL_SCHEMA_OBJECTS_SHA256,
    }
    if actual != expected:
        raise RuntimeError("v1.1 literal prompt/runtime contract changed: " + canonical_json({"actual": actual, "expected": expected}))


def _manifest_preflight(path: Path, repo_root: Path):
    e2_spec, e3_spec = default_specs(repo_root)
    result = validate_manifest(
        path,
        e2_spec=e2_spec,
        e3_spec=e3_spec,
        prior_specs=default_prior_specs(repo_root),
        expected_seed=CANONICAL_SAMPLE_SEED,
    )
    if not result.passed:
        raise RuntimeError("mandatory independent coverage preflight failed: " + canonical_json(result.as_record()))
    return result


def _review_cache_preflight(path: Path, *, resume: bool) -> None:
    if resume:
        if not path.is_dir():
            raise FileNotFoundError(f"resume requires its existing review cache: {path}")
        return
    if path.exists():
        raise FileExistsError(f"fresh v1.1 run refuses existing review cache: {path}")


def _resolve_verified_codex(explicit: Path | None) -> tuple[Path, str]:
    """Resolve a real Codex binary and bind its supported CLI version."""

    path = (
        explicit.expanduser().resolve()
        if explicit is not None
        else discover_codex_bin().resolve()
    )
    if not path.is_file():
        raise FileNotFoundError(f"Codex CLI does not exist: {path}")
    version = codex_version(path)
    require_supported_cli(version)
    return path, version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the byte-pinned disjoint 53-image semantic N10 v1.1.")
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
    root = args.repo_root.resolve()
    try:
        _assert_literal_contract()
        _review_cache_preflight(args.review_cache_dir.resolve(), resume=args.resume)
        codex_bin, actual_cli_version = _resolve_verified_codex(args.codex_bin)
        preflight = _manifest_preflight(args.manifest.resolve(), root)

        def mandatory_preflight(path: Path):
            return _manifest_preflight(path, root)

        result = run_semantic_vision_n10_v11(
            population=POPULATION,
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
        validation = validate_semantic_n10_v11(
            args.output_db, args.report, args.manifest, args.e2, args.e3,
            args.review_cache_dir, contract=VALIDATION,
        )
        if not validation.passed:
            raise RuntimeError("mandatory independent terminal validation failed: " + canonical_json(validation.as_dict()))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__, "status": "error"}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "already_complete": result.already_complete,
        "codex_bin": str(codex_bin),
        "codex_cli_version": actual_cli_version,
        "runner_cli_version": CLI_VERSION,
        "approved_envelope": {
            "model_call_reservations": MAX_MODEL_CALL_RESERVATIONS,
            "observed_input_token_stop_threshold": OBSERVED_INPUT_TOKEN_STOP_THRESHOLD,
            "observed_output_token_stop_threshold": OBSERVED_OUTPUT_TOKEN_STOP_THRESHOLD,
            "observed_total_token_stop_threshold": OBSERVED_TOTAL_TOKEN_STOP_THRESHOLD,
        },
        "coverage_preflight_passed": preflight.passed,
        "independent_terminal_validation_passed": validation.passed,
        "logical_sha256": result.logical_sha256,
        "metrics": result.metrics,
        "network_requests": result.physical_http_reservations,
        "output_db": str(result.output_db),
        "report": str(result.report_path),
        "resumed": result.resumed,
        "review_cache_dir": str(args.review_cache_dir.resolve()),
        "status": result.status,
        "vision_requests": result.model_call_reservations,
    }, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
