#!/usr/bin/env python
"""Build the WP2.2 country-normalized v3 identity candidate artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_candidates_v3 import (  # noqa: E402
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    build_identity_candidates_v3,
)


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Build WP2.2 country-normalized identity candidate v3.")
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest",type=Path,default=DEFAULT_MANIFEST)
    parser.add_argument("--report",type=Path,default=DEFAULT_REPORT)
    parser.add_argument("--skip-full-hash",action="store_true",help="Tests only; production must not use this.")
    args=parser.parse_args(argv)
    result=build_identity_candidates_v3(repo_root=ROOT,output_path=args.output,manifest_path=args.manifest,
        report_path=args.report,verify_full_hashes=not args.skip_full_hash)
    print(json.dumps({
        "action":"build_wp22_identity_candidates_v3","status":"PASS","run_id":result.run_id,
        "output":str(result.output_path),"candidate_count":result.candidate_count,
        "auto_merge_eligible_count":result.auto_merge_eligible_count,
        "changed_transition_count":result.changed_transition_count,"sample_count":result.sample_count,
        "stage_a_logical_sha256":result.logical_sha256,"full_logical_sha256":result.full_logical_sha256,
        "elapsed_seconds":round(result.elapsed_seconds,3),"network_requests":0,"vision_requests":0,"llm_requests":0,
    },ensure_ascii=False,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
