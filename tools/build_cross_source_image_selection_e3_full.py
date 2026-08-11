"""Preflight or explicitly execute the offline full-population E3 builder."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_image_selection_full_pipeline import (  # noqa: E402
    DEFAULT_CHECKPOINT_BUILDINGS,
    FULL_CONFIRMATION,
    FullBuildConfig,
    build_full_cross_source_image_selection,
    default_e2_path,
    preflight_full_cross_source_image_selection,
)
from canonical.cross_source_image_selection_pipeline import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_E2_BUILDER_VERSION,
    DEFAULT_E2_CONTRACT_VERSION,
    DEFAULT_E2_LOGICAL_SHA256,
    DEFAULT_E2_SHA256,
    DEFAULT_E2_SIZE,
    DEFAULT_SHORTLIST_SIZE,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only E3 full preflight by default. Actual materialization "
            "requires --execute-full plus the literal confirmation token."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--e2", type=Path, default=default_e2_path(Path.cwd()))
    parser.add_argument("--expected-e2-size", type=int, default=DEFAULT_E2_SIZE)
    parser.add_argument("--expected-e2-sha256", default=DEFAULT_E2_SHA256)
    parser.add_argument(
        "--expected-e2-logical-sha256", default=DEFAULT_E2_LOGICAL_SHA256
    )
    parser.add_argument(
        "--expected-e2-contract-version", default=DEFAULT_E2_CONTRACT_VERSION
    )
    parser.add_argument(
        "--expected-e2-builder-version", default=DEFAULT_E2_BUILDER_VERSION
    )
    parser.add_argument("--shortlist-size", type=int, default=DEFAULT_SHORTLIST_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--checkpoint-buildings",
        type=int,
        default=DEFAULT_CHECKPOINT_BUILDINGS,
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume only an exact matching non-terminal building artifact",
    )
    parser.add_argument(
        "--execute-full",
        action="store_true",
        help="execute the offline full build instead of read-only preflight",
    )
    parser.add_argument(
        "--confirm-full-materialization",
        metavar="LITERAL",
        help=f"must equal {FULL_CONFIRMATION!r} with --execute-full",
    )
    return parser


def _config(arguments: argparse.Namespace) -> FullBuildConfig:
    return FullBuildConfig(
        e2_path=arguments.e2,
        output_path=arguments.output,
        expected_e2_size=arguments.expected_e2_size,
        expected_e2_sha256=arguments.expected_e2_sha256,
        expected_e2_logical_sha256=arguments.expected_e2_logical_sha256,
        expected_e2_contract_version=arguments.expected_e2_contract_version,
        expected_e2_builder_version=arguments.expected_e2_builder_version,
        shortlist_size=arguments.shortlist_size,
        batch_size=arguments.batch_size,
        checkpoint_buildings=arguments.checkpoint_buildings,
        resume=arguments.resume,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.execute_full:
        if arguments.confirm_full_materialization != FULL_CONFIRMATION:
            parser.error(
                "--execute-full requires --confirm-full-materialization "
                + FULL_CONFIRMATION
            )
    elif arguments.confirm_full_materialization is not None:
        parser.error("confirmation token is valid only with --execute-full")

    config = _config(arguments)
    if not arguments.execute_full:
        result = preflight_full_cross_source_image_selection(config)
        payload = {
            "action": "preflight",
            "candidate_occurrence_minus_unique_asset_count": (
                result.candidate_occurrence_minus_unique_asset_count
            ),
            "authorized": False,
            "creates_output": False,
            "e2_byte_sha256": result.e2_byte_sha256,
            "e2_logical_sha256": result.e2_logical_sha256,
            "e2_path": str(result.e2_path),
            "e2_size_bytes": result.e2_size_bytes,
            "disk_free_bytes": result.disk_free_bytes,
            "estimated_output_bytes": result.estimated_output_bytes,
            "eligible_buildings": result.eligible_buildings,
            "image_candidates": result.image_candidates,
            "llm_requests": 0,
            "network_requests": 0,
            "no_clobber_ready": result.no_clobber_ready,
            "output_exists": result.output_exists,
            "output_lock_exists": result.output_lock_exists,
            "output_parent": str(result.output_parent),
            "output_path": str(result.output_path),
            "output_sqlite_sidecars": list(result.output_sqlite_sidecars),
            "population_buildings": result.population_buildings,
            "minimum_free_bytes": result.minimum_free_bytes,
            "minimum_disk_space_satisfied": result.minimum_disk_space_satisfied,
            "recommended_free_bytes": result.recommended_free_bytes,
            "recommended_disk_space_satisfied": (
                result.recommended_disk_space_satisfied
            ),
            "same_building_direct_edges": result.same_building_direct_edges,
            "unique_success_assets": result.unique_success_assets,
            "vision_requests": 0,
        }
    else:
        result = build_full_cross_source_image_selection(config)
        payload = {
            "action": "full_build",
            "authorized": True,
            "elapsed_seconds": result.elapsed_seconds,
            "eligible_buildings": result.eligible_buildings,
            "image_candidates": result.image_candidates,
            "llm_requests": 0,
            "logical_sha256": result.logical_sha256,
            "network_requests": 0,
            "output_path": str(result.output_path),
            "population_buildings": result.population_buildings,
            "resumed": result.resumed,
            "run_id": result.run_id,
            "shortlist_items": result.shortlist_items,
            "status": result.status,
            "vision_requests": 0,
        }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
