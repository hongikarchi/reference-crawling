#!/usr/bin/env python3
"""Build the offline, disjoint semantic-coverage N10 v1.1 manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage_n10_v11 import (  # noqa: E402
    DEFAULT_SAMPLE_SEED,
    build_manifest,
    default_prior_specs,
    write_manifest,
)
from canonical.cross_source_semantic_coverage_sources import default_specs  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan fixed N10 v1.1 from immutable E2/E3 with zero network/model calls."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--sample-seed", default=DEFAULT_SAMPLE_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        e2, e3 = default_specs(root)
        manifest = build_manifest(
            e2, e3, default_prior_specs(root), seed=args.sample_seed
        )
        output = write_manifest(args.output, manifest)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "llm_requests": 0,
                    "network_requests": 0,
                    "status": "error",
                    "vision_requests": 0,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "llm_requests": 0,
                "manifest_sha256": manifest["semantic_coverage_manifest_sha256"],
                "network_requests": 0,
                "output_path": str(output),
                "planned_occurrence_count": manifest["planned_occurrence_count"],
                "planned_unique_e1_pixel_count": manifest["planned_unique_e1_pixel_count"],
                "sample_size_buildings": manifest["sample_size_buildings"],
                "status": "complete",
                "vision_requests": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
