#!/usr/bin/env python
"""Independently validate the approved terminal semantic N10 v1.1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_vision_n10_v11_validator import (  # noqa: E402
    ValidationContract,
    validate_semantic_n10_v11,
)


DEFAULT_MANIFEST = Path("data/reports/cross_source_semantic_coverage_n10_v1_1.json")
DEFAULT_E2 = Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db")
DEFAULT_E3 = Path("data/enrichment/divisare_architizer_image_selection_e3_full_v1.db")
DEFAULT_OUTPUT = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db")
DEFAULT_REPORT = Path("data/reports/divisare_architizer_semantic_vision_n10_v1_1.md")
DEFAULT_REVIEW_CACHE = Path("data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_cache")

# Literal duplication is deliberate: this CLI must not inherit production
# identities from the runner it is validating.
CONTRACT = ValidationContract(
    manifest_domain="archibe-cross-source-semantic-coverage-n10-v1.1-manifest-v1",
    manifest_byte_sha256="3315a48188aec4c9461867235999ac7e526b8b011a869cafdd3be9d3cd1cc5b6",
    manifest_self_sha256="91957a822915ae70f56fd1a5f82244098a4508274cc5dbbdd91e1d6bf03ad8c9",
    sample_seed="archibe-semantic-coverage-n10-v1.1",
    occurrence_count=53,
    ordered_building_manifest_sha256="7b4c9558682f231e4fce29d0e915666fe1968707e1fea177d58ac0ad514af55d",
    ordered_occurrence_manifest_sha256="0be0449356388c117e59856caacdf14900b694b0c1ee811f807b956c9449fcb6",
    ordered_fetch_url_manifest_sha256="9de60a38b4ef5e05744c0761c13e84d0a33fe88df6e09ea4b8c9ea24ac527c43",
    ordered_e1_pixel_manifest_sha256="0ec3a756a3a55480b077e6eb114f4b2df9d1497f0b4dd015e53008e1783324a8",
    inference_id_prefix="semv11_",
    max_model_call_reservations=13,
    observed_input_token_stop_threshold=260_000,
    observed_output_token_stop_threshold=10_000,
    observed_total_token_stop_threshold=270_000,
    expected_schema_sql_sha256="85b12261ce7c720c21bbad74669c7bc89b0c7e185ba259ff9a552e15608df31e",
    expected_schema_objects_sha256="959f95fe3a3ae9f6daa6ee9978eedc9abc00a5bad6c7fe6ef577ac03c02c3fe1",
    expected_prompt_template_sha256="42b43a9168ea3315809182da5ff6a5ecdf280cbaabbfa2a369a88806cc7d50ba",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only independent validation of semantic N10 v1.1.")
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
        result = validate_semantic_n10_v11(
            args.sidecar, args.report, args.manifest, args.e2, args.e3,
            args.review_cache_dir, contract=CONTRACT,
        )
    except Exception as exc:
        print(json.dumps({
            "error": str(exc), "error_type": type(exc).__name__,
            "llm_requests": 0, "network_requests": 0,
            "passed": False, "vision_requests": 0,
        }, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
