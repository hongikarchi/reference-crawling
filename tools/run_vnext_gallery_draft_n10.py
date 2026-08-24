#!/usr/bin/env python
"""Build the offline WP1 Stage A vNext N10 gallery draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_draft import (  # noqa: E402
    DEFAULT_FREEZE,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    build_gallery_draft,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline N10 exact-pixel/direct-star gallery draft; network/Vision/LLM are always zero."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    args = parser.parse_args(argv)
    result = build_gallery_draft(
        repo_root=ROOT,
        output_path=args.output,
        manifest_path=args.manifest,
        report_path=args.report,
        freeze_path=args.freeze,
        verify_full_hashes=True,
    )
    print(
        json.dumps(
            {
                "action": "wp1_stage_a",
                "status": "PASS",
                "run_id": result.run_id,
                "output_path": str(result.output_path),
                "manifest_path": str(result.manifest_path),
                "report_path": str(result.report_path),
                "logical_sha256": result.logical_sha256,
                "building_count": result.building_count,
                "input_asset_count": result.input_asset_count,
                "gallery_asset_count": result.gallery_asset_count,
                "gallery_group_count": result.gallery_group_count,
                "exclusion_count": result.exclusion_count,
                "unresolved_count": result.unresolved_count,
                "direct_attachment_count": result.direct_attachment_count,
                "network_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
