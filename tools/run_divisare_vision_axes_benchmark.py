#!/usr/bin/env python3
"""Run a frozen 1024px Divisare Vision axis benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_benchmark import (  # noqa: E402
    SUPPORTED_LIMITS,
    run_axes_benchmark,
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


DEFAULT_SOURCE = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Divisare image-axis benchmark at a 1024px long edge. "
            "Development gold supports N10/N20/N50; fresh holdout gold permits "
            "one N50 run and derives N10/N20 from that result."
        )
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--gold-manifest", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=SUPPORTED_LIMITS, required=True)
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    codex_bin = (args.codex_bin or discover_codex_bin()).resolve()
    cli_version = codex_version(codex_bin)
    require_supported_cli(cli_version)
    result = run_axes_benchmark(
        source_db=args.source_db,
        gold_manifest_path=args.gold_manifest,
        output_db=args.output_db,
        report_path=args.report,
        limit=args.limit,
        codex_bin=codex_bin,
        model=args.model,
        reasoning=args.reasoning,
        service_tier=args.service_tier,
        cli_version=cli_version,
        resume=args.resume,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["technical_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
