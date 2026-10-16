#!/usr/bin/env python
"""Independently validate the terminal semantic N10 v1.2 artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_vision_n10_v12_validator import (  # noqa: E402
    ValidationContract,
    validate_semantic_n10_v12,
)


DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_2.json")
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_E3 = Path("data/enrichment/divisare_architizer_image_selection_e3_full_v1.db")
DEFAULT_OUTPUT = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db")
DEFAULT_REPORT = Path("data/reports/divisare_architizer_semantic_vision_n10_v1_2.md")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_cache")


PRODUCTION_CONTRACT: ValidationContract | None = ValidationContract(
    expected_run_id="semn10v12-82ddad3e9cb37d3f640209c7",
    manifest_domain="archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1",
    manifest_byte_sha256="00d494c58590a9a0ca3d78de858f857f6a1e226390018f5426d799a846ead8fe",
    manifest_self_sha256="44f9c5ca47adbacd8fc0f87be11e59b6468d57205831e2c5d78b0f96438a495a",
    sample_seed="archibe-semantic-coverage-n10-v1.2",
    occurrence_count=50,
    ordered_building_manifest_sha256="8008b9972ce779488e280a0709013a3a13916d9936ce1aaafb1fa1a13c3363fb",
    ordered_occurrence_manifest_sha256="939cea9f514a0caeb4fa4ec7bc9688f062ae212318f4d458ae551b04574cc314",
    ordered_fetch_url_manifest_sha256="1cf542b62edd93defd375d1e070f06975bae92ce4b5d80a2834a63d77e990d3e",
    ordered_e1_pixel_manifest_sha256="613bce3f755497e9944c7368c6d720bd913a7ee58d1523afef2bef09d3e05d5c",
    inference_id_prefix="semv12_",
    max_model_call_reservations=12,
    observed_input_token_stop_threshold=250_000,
    observed_output_token_stop_threshold=10_000,
    observed_total_token_stop_threshold=260_000,
    expected_schema_sql_sha256="7eebb5135000125d3e2a453bf48526d28518fe78fe2d861672b18b30bd382640",
    expected_schema_objects_sha256="e77a4be8896aa474720155e217162a7592d0f5a6018c3b8c2c5ea1f09efc2401",
    expected_prior_run_names=(
        "semantic_n10_v1",
        "semantic_n100_v1",
        "semantic_n10_v1_1",
    ),
    expected_prior_successful_derivative_count=665,
    expected_model="gpt-5.6-sol",
    expected_reasoning="low",
    expected_service_tier="fast",
    expected_image_detail="high",
    expected_cli_version="codex-cli 0.147.0",
    expected_prompt_template_sha256="041d6b7b0c6de4abc9989c585cd7a45267932eb6812f0225e0d12f11b99f7d73",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only independent validation of semantic N10 v1.2.")
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--e2", type=Path, default=DEFAULT_E2)
    parser.add_argument("--e3", type=Path, default=DEFAULT_E3)
    parser.add_argument("--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if PRODUCTION_CONTRACT is None:
            raise RuntimeError("v1.2 independent production pins are not finalized")
        result = validate_semantic_n10_v12(
            args.sidecar,
            args.report,
            args.manifest,
            args.e2,
            args.e3,
            args.review_cache_dir,
            contract=PRODUCTION_CONTRACT,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "llm_requests": 0,
                    "network_requests": 0,
                    "passed": False,
                    "vision_requests": 0,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
