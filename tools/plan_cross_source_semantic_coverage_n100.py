#!/usr/bin/env python3
"""Create the offline, fixed-seed population-proportional N100 manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage_n100 import (  # noqa: E402
    DEFAULT_N100_SAMPLE_SEED,
    DEFAULT_N10_MANIFEST_BYTE_SHA256,
    DEFAULT_N10_MANIFEST_RELATIVE_PATH,
    DEFAULT_N10_MANIFEST_SELF_SHA256,
    DEFAULT_N10_MANIFEST_SIZE,
    DEFAULT_N10_RUN_ID,
    DEFAULT_N10_SIDECAR_BYTE_SHA256,
    DEFAULT_N10_SIDECAR_LOGICAL_SHA256,
    DEFAULT_N10_SIDECAR_RELATIVE_PATH,
    DEFAULT_N10_SIDECAR_SIZE,
    PriorN10Spec,
    build_semantic_coverage_n100_manifest,
    write_semantic_coverage_n100_manifest,
)
from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    DEFAULT_E2_APPLICATION_ID,
    DEFAULT_E2_LOGICAL_SHA256,
    DEFAULT_E2_RELATIVE_PATH,
    DEFAULT_E2_RUN_ID,
    DEFAULT_E2_SHA256,
    DEFAULT_E2_SIZE,
    DEFAULT_E3_APPLICATION_ID,
    DEFAULT_E3_LOGICAL_SHA256,
    DEFAULT_E3_RELATIVE_PATH,
    DEFAULT_E3_RUN_ID,
    DEFAULT_E3_SHA256,
    DEFAULT_E3_SIZE,
    DEFAULT_USER_VERSION,
    ArtifactSpec,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _sha(value: str) -> str:
    normalized = value.casefold()
    if len(normalized) != 64:
        raise argparse.ArgumentTypeError("SHA-256 must have 64 hex characters")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("SHA-256 must be hexadecimal") from exc
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan a deterministic population-proportional 100-building semantic "
            "coverage manifest from immutable E2/E3 and completed N10."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--sample-seed", default=DEFAULT_N100_SAMPLE_SEED)
    parser.add_argument("--e2", type=Path)
    parser.add_argument("--e3", type=Path)
    parser.add_argument("--n10-manifest", type=Path)
    parser.add_argument("--n10-sidecar", type=Path)
    parser.add_argument("--expected-e2-size", type=_positive_int, default=DEFAULT_E2_SIZE)
    parser.add_argument("--expected-e2-sha256", type=_sha, default=DEFAULT_E2_SHA256)
    parser.add_argument("--expected-e2-logical-sha256", type=_sha, default=DEFAULT_E2_LOGICAL_SHA256)
    parser.add_argument("--expected-e2-run-id", default=DEFAULT_E2_RUN_ID)
    parser.add_argument("--expected-e2-application-id", type=int, default=DEFAULT_E2_APPLICATION_ID)
    parser.add_argument("--expected-e3-size", type=_positive_int, default=DEFAULT_E3_SIZE)
    parser.add_argument("--expected-e3-sha256", type=_sha, default=DEFAULT_E3_SHA256)
    parser.add_argument("--expected-e3-logical-sha256", type=_sha, default=DEFAULT_E3_LOGICAL_SHA256)
    parser.add_argument("--expected-e3-run-id", default=DEFAULT_E3_RUN_ID)
    parser.add_argument("--expected-e3-application-id", type=int, default=DEFAULT_E3_APPLICATION_ID)
    parser.add_argument("--expected-user-version", type=int, default=DEFAULT_USER_VERSION)
    parser.add_argument("--expected-n10-manifest-size", type=_positive_int, default=DEFAULT_N10_MANIFEST_SIZE)
    parser.add_argument("--expected-n10-manifest-sha256", type=_sha, default=DEFAULT_N10_MANIFEST_BYTE_SHA256)
    parser.add_argument("--expected-n10-manifest-self-sha256", type=_sha, default=DEFAULT_N10_MANIFEST_SELF_SHA256)
    parser.add_argument("--expected-n10-sidecar-size", type=_positive_int, default=DEFAULT_N10_SIDECAR_SIZE)
    parser.add_argument("--expected-n10-sidecar-sha256", type=_sha, default=DEFAULT_N10_SIDECAR_BYTE_SHA256)
    parser.add_argument("--expected-n10-sidecar-logical-sha256", type=_sha, default=DEFAULT_N10_SIDECAR_LOGICAL_SHA256)
    parser.add_argument("--expected-n10-run-id", default=DEFAULT_N10_RUN_ID)
    return parser


def _specs(args: argparse.Namespace) -> tuple[ArtifactSpec, ArtifactSpec, PriorN10Spec]:
    root = args.repo_root.resolve()
    e2_path = (args.e2 or root / DEFAULT_E2_RELATIVE_PATH).resolve()
    e3_path = (args.e3 or root / DEFAULT_E3_RELATIVE_PATH).resolve()
    e2 = ArtifactSpec(
        "e2_evidence", e2_path, args.expected_e2_size,
        args.expected_e2_sha256, args.expected_e2_logical_sha256,
        args.expected_e2_run_id, args.expected_e2_application_id,
        args.expected_user_version,
    )
    e3 = ArtifactSpec(
        "e3_selection", e3_path, args.expected_e3_size,
        args.expected_e3_sha256, args.expected_e3_logical_sha256,
        args.expected_e3_run_id, args.expected_e3_application_id,
        args.expected_user_version,
    )
    n10 = PriorN10Spec(
        manifest_path=(args.n10_manifest or root / DEFAULT_N10_MANIFEST_RELATIVE_PATH).resolve(),
        sidecar_path=(args.n10_sidecar or root / DEFAULT_N10_SIDECAR_RELATIVE_PATH).resolve(),
        expected_manifest_size=args.expected_n10_manifest_size,
        expected_manifest_byte_sha256=args.expected_n10_manifest_sha256,
        expected_manifest_self_sha256=args.expected_n10_manifest_self_sha256,
        expected_sidecar_size=args.expected_n10_sidecar_size,
        expected_sidecar_byte_sha256=args.expected_n10_sidecar_sha256,
        expected_sidecar_logical_sha256=args.expected_n10_sidecar_logical_sha256,
        expected_run_id=args.expected_n10_run_id,
    )
    return e2, e3, n10


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        e2, e3, n10 = _specs(args)
        manifest = build_semantic_coverage_n100_manifest(
            e2, e3, n10, seed=args.sample_seed
        )
        output = write_semantic_coverage_n100_manifest(args.output, manifest)
    except Exception as exc:
        print(json.dumps({
            "error": str(exc), "error_type": type(exc).__name__,
            "llm_requests": 0, "network_requests": 0,
            "status": "error", "vision_requests": 0,
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "llm_requests": 0,
        "manifest_sha256": manifest["semantic_coverage_manifest_sha256"],
        "network_requests": 0,
        "output_path": str(output),
        "planned_occurrence_count": manifest["planned_occurrence_count"],
        "planned_unique_e1_pixel_count": manifest["planned_unique_e1_pixel_count"],
        "sample_size_buildings": manifest["sample_size_buildings"],
        "status": "complete",
        "vision_requests": 0,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
