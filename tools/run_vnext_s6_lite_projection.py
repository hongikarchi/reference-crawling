#!/usr/bin/env python
"""Build the offline vNext S6-lite building_images projection (legacy identity, S2 gallery)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical import vnext_s6_lite_projection as s6  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, default=s6.DEFAULT_LEGACY)
    parser.add_argument("--s2-db", type=Path, default=s6.DEFAULT_S2_DB)
    parser.add_argument("--s2-manifest", type=Path, default=s6.DEFAULT_S2_MANIFEST)
    parser.add_argument("--s2-validation", type=Path, default=s6.DEFAULT_S2_VALIDATION)
    parser.add_argument("--e2", type=Path, default=s6.DEFAULT_E2)
    parser.add_argument("--ddl", type=Path, default=s6.DEFAULT_DDL)
    parser.add_argument("--output", type=Path, default=s6.DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=s6.DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=s6.DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = s6.build_projection(
        repo_root=ROOT, legacy_path=args.legacy, s2_db_path=args.s2_db, s2_manifest_path=args.s2_manifest,
        s2_validation_path=args.s2_validation, e2_path=args.e2, ddl_path=args.ddl, output_path=args.output,
        manifest_path=args.manifest, report_path=args.report,
    )
    print(json.dumps({
        "action": "vnext_s6_lite_projection", "status": "PASS", "run_id": result.run_id,
        "rows": result.row_count, "ordered_rowset_sha256": result.ordered_rowset_sha256,
        "output_sha256": result.output_sha256, "output": str(result.output_path),
        "manifest": str(result.manifest_path), "report": str(result.report_path),
        "elapsed_seconds": round(result.elapsed_seconds, 3), "network": 0, "llm": 0, "vision": 0, "db_writes": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
