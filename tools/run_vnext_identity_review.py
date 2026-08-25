#!/usr/bin/env python
"""Run WP2 Stage B, apply blind review, or apply adjudication."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_review import (  # noqa: E402
    DEFAULT_ADJUDICATION_FORM,
    DEFAULT_ADJUDICATION_PACKET,
    DEFAULT_ARTIFACT,
    DEFAULT_CACHE,
    DEFAULT_GATE_REPORT,
    DEFAULT_REVIEW_FORM,
    DEFAULT_REVIEW_PREFIX,
    DEFAULT_STAGE_B_MANIFEST,
    DEFAULT_STAGE_B_REPORT,
    apply_adjudication,
    apply_blind_review,
    requeue_connection_failures,
    run_stage_b,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WP2 HTTP-only blind identity review workflow.")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="Freeze review assets, reuse cache/fetch HTTP, and render blind sheets.")
    prepare.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    prepare.add_argument("--review-prefix", type=Path, default=DEFAULT_REVIEW_PREFIX)
    prepare.add_argument("--review-form", type=Path, default=DEFAULT_REVIEW_FORM)
    prepare.add_argument("--manifest", type=Path, default=DEFAULT_STAGE_B_MANIFEST)
    prepare.add_argument("--report", type=Path, default=DEFAULT_STAGE_B_REPORT)
    prepare.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    prepare.add_argument("--workers", type=int, default=4)
    prepare.add_argument("--skip-full-hash", action="store_true", help="Tests only; production must not use this.")
    requeue = sub.add_parser("requeue-connection-failures", help="Archive an all-connection-failed attempt and requeue it.")
    requeue.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    review = sub.add_parser("apply-review", help="Lock the completed blind review and emit the unblinded adjudication packet.")
    review.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    review.add_argument("--review-form", type=Path, default=DEFAULT_REVIEW_FORM)
    review.add_argument("--packet", type=Path, default=DEFAULT_ADJUDICATION_PACKET)
    review.add_argument("--adjudication-form", type=Path, default=DEFAULT_ADJUDICATION_FORM)
    adjudicate = sub.add_parser("apply-adjudication", help="Lock adjudication and evaluate the lane-C gate.")
    adjudicate.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    adjudicate.add_argument("--adjudication-form", type=Path, default=DEFAULT_ADJUDICATION_FORM)
    adjudicate.add_argument("--gate-report", type=Path, default=DEFAULT_GATE_REPORT)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        result = run_stage_b(
            repo_root=ROOT, artifact_path=args.artifact, review_prefix=args.review_prefix,
            review_form=args.review_form, manifest_path=args.manifest, report_path=args.report,
            cache_root=args.cache_root, workers=args.workers,
            verify_full_hashes=not args.skip_full_hash,
        )
        payload = {
            "action":"wp2_stage_b_prepare","status":"PASS","run_id":result.run_id,
            "selected_asset_count":result.selected_asset_count,"cache_hit_count":result.cache_hit_count,
            "logical_fetch_count":result.logical_fetch_count,"http_request_count":result.http_request_count,
            "pixel_status_counts":result.pixel_status_counts,"review_sheet_count":len(result.review_sheet_paths),
            "review_form_path":str(result.review_form_path),"vision_requests":0,"llm_requests":0,
            "elapsed_seconds":round(result.elapsed_seconds,3),
        }
    elif args.action == "requeue-connection-failures":
        run_id,attempt_no,asset_count=requeue_connection_failures(repo_root=ROOT,artifact_path=args.artifact)
        payload={"action":"wp2_requeue_connection_failures","status":"PASS","run_id":run_id,
            "archived_attempt_no":attempt_no,"requeued_asset_count":asset_count}
    elif args.action == "apply-review":
        packet, form = apply_blind_review(
            repo_root=ROOT,artifact_path=args.artifact,review_form_path=args.review_form,
            adjudication_packet_path=args.packet,adjudication_form_path=args.adjudication_form,
        )
        payload={"action":"wp2_apply_blind_review","status":"PASS","adjudication_packet":str(packet),"adjudication_form":str(form)}
    else:
        result=apply_adjudication(
            repo_root=ROOT,artifact_path=args.artifact,adjudication_form_path=args.adjudication_form,
            gate_report_path=args.gate_report,
        )
        payload={"action":"wp2_apply_adjudication","status":result.status,"run_id":result.run_id,
            "false_merge_count":result.false_merge_count,"hard_conflict_violation_count":result.hard_conflict_violation_count,
            "disagreement_count":result.disagreement_count,"gate_report":str(result.report_path)}
    print(json.dumps(payload,ensure_ascii=False,sort_keys=True))
    return 0 if payload["status"]=="PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
