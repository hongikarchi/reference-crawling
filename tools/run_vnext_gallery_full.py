#!/usr/bin/env python
"""Build the offline vNext WP4 S2 full gallery v1 (N10 / N1000 ladder or full 91,803)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_full import (  # noqa: E402
    DEFAULT_CONTINUITY_SUMMARY,
    DEFAULT_CONTRACT,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    build_gallery_full,
)
from canonical.vnext_gallery_draft import DEFAULT_FREEZE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline S2 full gallery (exact-class only, deferred direct edges); network/Vision/LLM are always zero."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--continuity-summary", type=Path, default=DEFAULT_CONTINUITY_SUMMARY)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--limit-buildings", type=int, default=None,
                        help="ladder smoke: deterministic hash-seeded subset of N buildings (omit for full)")
    args = parser.parse_args(argv)
    if args.limit_buildings is None and (args.output != DEFAULT_OUTPUT):
        print("note: full scope written to a non-default output path", file=sys.stderr)
    if args.limit_buildings is not None and args.output == DEFAULT_OUTPUT:
        parser.error("ladder runs must use a separate --output/--manifest/--report path")

    def progress(done: int, total: int, seconds: float) -> None:
        print(f"progress {done}/{total} buildings {seconds:.1f}s", file=sys.stderr, flush=True)

    result = build_gallery_full(
        repo_root=ROOT,
        output_path=args.output,
        manifest_path=args.manifest,
        report_path=args.report,
        freeze_path=args.freeze,
        continuity_summary_path=args.continuity_summary,
        contract_path=args.contract,
        limit_buildings=args.limit_buildings,
        verify_full_hashes=True,
        progress=progress,
    )
    print(
        json.dumps(
            {
                "action": "wp4_s2_full",
                "status": "PASS",
                "run_id": result.run_id,
                "scope": result.scope,
                "output_path": str(result.output_path),
                "manifest_path": str(result.manifest_path),
                "report_path": str(result.report_path),
                "logical_sha256": result.logical_sha256,
                "building_count": result.building_count,
                "totals": dict(result.totals),
                "per_source": {key: dict(value) for key, value in result.per_source.items()},
                "network_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "phase_seconds": {key: round(value, 3) for key, value in result.phase_seconds.items()},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
