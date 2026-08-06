#!/usr/bin/env python3
"""Build the immutable metadata-only Divisare Vision N100 candidate pool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_gold import write_candidate_manifest  # noqa: E402


DEFAULT_SOURCE = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic over-sampled Divisare Vision review pool."
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = write_candidate_manifest(args.source_db, args.output)
    counts: dict[str, int] = {}
    for row in payload["candidates"]:
        key = "%s/%s" % (row["discovery_class"], row["generation_group"])
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "manifest_sha256": payload["manifest_sha256"],
                "candidate_count": len(payload["candidates"]),
                "counts": counts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
