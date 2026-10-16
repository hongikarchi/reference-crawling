#!/usr/bin/env python
"""Independently validate the immutable cross-source semantic Vision N100."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_vision_n100_validator import (  # noqa: E402
    validate_semantic_vision_n100,
)


DEFAULT_SIDECAR = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n100_v1.db"
)
DEFAULT_REPORT = Path(
    "data/reports/divisare_architizer_semantic_vision_n100_v1.md"
)
DEFAULT_MANIFEST = Path(
    "data/reports/cross_source_semantic_coverage_n100_v1.json"
)
DEFAULT_E2 = Path(
    "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
)
DEFAULT_E3 = Path(
    "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db"
)
DEFAULT_N10_MANIFEST = Path(
    "data/reports/cross_source_semantic_coverage_n10_v1.json"
)
DEFAULT_N10_SIDECAR = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db"
)
DEFAULT_REVIEW_CACHE = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n100_v1_review_cache"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the frozen N100 manifest, immutable input lineage, SQLite "
            "ledger, raw JSONL, semantic rows, derivatives, logical digest, "
            "and report without network or model access."
        )
    )
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--e2", type=Path, default=DEFAULT_E2)
    parser.add_argument("--e3", type=Path, default=DEFAULT_E3)
    parser.add_argument(
        "--n10-manifest", type=Path, default=DEFAULT_N10_MANIFEST
    )
    parser.add_argument("--n10-sidecar", type=Path, default=DEFAULT_N10_SIDECAR)
    parser.add_argument(
        "--review-cache-dir", type=Path, default=DEFAULT_REVIEW_CACHE
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_semantic_vision_n100(
            args.sidecar,
            report_path=args.report,
            manifest_path=args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
            n10_manifest_path=args.n10_manifest,
            n10_sidecar_path=args.n10_sidecar,
            review_cache_dir=args.review_cache_dir,
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
