#!/usr/bin/env python3
"""Build the fresh, metadata-only Divisare Vision-axis holdout pool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_image_smoke import file_sha256  # noqa: E402
from canonical.divisare_vision_axes_holdout import (  # noqa: E402
    write_candidate_manifest,
)


DEFAULT_SOURCE = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"
DEFAULT_EXCLUSION = (
    ROOT
    / "data"
    / "review"
    / "divisare_vision_gold_candidates_v1_2_probed.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "review"
    / "divisare_vision_axes_holdout_candidates_n100_v1.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build 100 deterministic Divisare Vision-axis holdout candidates "
            "from metadata only; this command performs no network requests."
        )
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--exclude-manifest", type=Path, default=DEFAULT_EXCLUSION
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    payload = write_candidate_manifest(
        args.source_db, args.exclude_manifest, args.output
    )
    metrics = payload["selection_metrics"]
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "output_file_sha256": file_sha256(args.output.resolve()),
                "manifest_version": payload["manifest_version"],
                "manifest_sha256": payload["manifest_sha256"],
                "source_db_sha256": payload["source_db_sha256"],
                "exclusion_manifest_sha256": payload["provenance"][
                    "exclusion_manifest_sha256"
                ],
                "candidate_count": metrics["candidate_count"],
                "proxy_counts": metrics["proxy_counts"],
                "generation_counts": metrics["generation_counts"],
                "role_counts": metrics["role_counts"],
                "oos_subtype_counts": metrics["oos_subtype_counts"],
                "selected_identity_set_sha256": metrics[
                    "selected_identity_set_sha256"
                ],
                "network_io": payload["contract"]["network_io"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
