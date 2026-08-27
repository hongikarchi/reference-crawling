#!/usr/bin/env python3
"""Run the no-network WP2.7 identity-instrument v2 contrast workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical import vnext_identity_instrument_v2_probe as probe


def _path(value: str) -> Path:
    return Path(value)


def _quota_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--remaining-percent", type=float, required=True)
    parser.add_argument("--observed-at", required=True, help="ISO-8601 timestamp with timezone")
    parser.add_argument("--quota-source", required=True)
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--limitation", default="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=_path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    stage_a = commands.add_parser("freeze-instrument", help="Stage A: freeze v2 contract")
    _quota_arguments(stage_a)
    stage_a.add_argument("--protocol", type=_path, default=probe.DEFAULT_PROTOCOL)
    stage_a.add_argument("--order", type=_path, default=probe.DEFAULT_ORDER)
    stage_a.add_argument("--source-db", type=_path, default=probe.DEFAULT_SOURCE_DB)
    stage_a.add_argument("--source-sample-manifest", type=_path,
                         default=probe.DEFAULT_SOURCE_SAMPLE_MANIFEST)
    stage_a.add_argument("--source-report", type=_path, default=probe.DEFAULT_SOURCE_REPORT)
    stage_a.add_argument("--source-manifest", type=_path, default=probe.DEFAULT_SOURCE_MANIFEST)
    stage_a.add_argument("--source-fetch-manifest", type=_path,
                         default=probe.DEFAULT_SOURCE_FETCH_MANIFEST)
    stage_a.add_argument("--source-fetch-correction", type=_path,
                         default=probe.DEFAULT_SOURCE_FETCH_CORRECTION)
    stage_a.add_argument("--source-producer", type=_path, default=probe.DEFAULT_SOURCE_PRODUCER)
    stage_a.add_argument("--source-adjudication-locked", type=_path,
                         default=probe.DEFAULT_SOURCE_ADJUDICATION_LOCKED)
    stage_a.add_argument("--output", type=_path, default=probe.DEFAULT_OUTPUT)

    stage_b = commands.add_parser("freeze-sample", help="Stage B: replay exact WP2.6 inputs")
    stage_b.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    stage_b.add_argument("--sample-manifest", type=_path, default=probe.DEFAULT_SAMPLE_MANIFEST)

    stage_c = commands.add_parser("declare-gate", help="Stage C: predeclare four predicates")
    stage_c.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)

    quota = commands.add_parser("record-quota", help="Record a pre-wave quota observation")
    quota.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    quota.add_argument("--phase", choices=probe.QUOTA_PHASES[1:], required=True)
    _quota_arguments(quota)

    prepare = commands.add_parser("prepare-review", help="Publish label-free reviewer forms")
    prepare.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    prepare.add_argument("--r1-form", type=_path, default=probe.DEFAULT_R1_FORM)
    prepare.add_argument("--r2-form", type=_path, default=probe.DEFAULT_R2_FORM)
    prepare.add_argument("--fetch-manifest", type=_path, default=probe.DEFAULT_FETCH_MANIFEST)
    prepare.add_argument("--fetch-report", type=_path, default=probe.DEFAULT_FETCH_REPORT)

    lock = commands.add_parser("lock-review", help="Validate and lock one reviewer axis ledger")
    lock.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    lock.add_argument("--lane", choices=("r1", "r2"), required=True)
    lock.add_argument("--completed-form", type=_path, required=True)
    lock.add_argument("--locked-output", type=_path, required=True)

    adj = commands.add_parser("prepare-adjudication", help="Publish blind axis adjudication packet")
    adj.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    adj.add_argument("--packet", type=_path, default=probe.DEFAULT_ADJUDICATION_PACKET)
    adj.add_argument("--adjudication-form", type=_path, default=probe.DEFAULT_ADJUDICATION_FORM)

    adj_lock = commands.add_parser("lock-adjudication", help="Validate and lock final axes")
    adj_lock.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    adj_lock.add_argument("--completed-form", type=_path, required=True)
    adj_lock.add_argument("--locked-output", type=_path, default=probe.DEFAULT_ADJUDICATION_LOCKED)

    reveal = commands.add_parser("reveal-score", help="Reveal controls and score terminal gate")
    reveal.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    reveal.add_argument("--report", type=_path, default=probe.DEFAULT_REPORT)
    reveal.add_argument("--manifest", type=_path, default=probe.DEFAULT_MANIFEST)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root
    if args.command == "freeze-instrument":
        result = probe.freeze_instrument(
            repo_root=root, quota_remaining_percent=args.remaining_percent,
            quota_observed_at=args.observed_at, quota_source=args.quota_source,
            quota_account_index=args.account_index, quota_limitation=args.limitation,
            protocol_path=args.protocol, order_path=args.order, source_db_path=args.source_db,
            source_sample_manifest_path=args.source_sample_manifest,
            source_report_path=args.source_report, source_manifest_path=args.source_manifest,
            source_fetch_manifest_path=args.source_fetch_manifest,
            source_fetch_correction_path=args.source_fetch_correction,
            source_producer_path=args.source_producer,
            source_adjudication_locked_path=args.source_adjudication_locked,
            output_path=args.output,
        )
        output = {"run_id": result.run_id, "status": result.status,
                  "artifact": str(result.artifact_path)}
    elif args.command == "freeze-sample":
        result = probe.freeze_sample(repo_root=root, artifact_path=args.artifact,
                                     sample_manifest_path=args.sample_manifest)
        output = {"run_id": result.run_id, "status": "sample_frozen",
                  "samples": result.sample_count, "sheets": result.sheet_count,
                  "asset_occurrences": result.asset_occurrence_count}
    elif args.command == "declare-gate":
        output = probe.declare_gate(repo_root=root, artifact_path=args.artifact)
    elif args.command == "record-quota":
        output = probe.record_quota_observation(
            repo_root=root, artifact_path=args.artifact, phase=args.phase,
            remaining_percent=args.remaining_percent, source=args.quota_source,
            observed_at=args.observed_at, account_index=args.account_index,
            limitation=args.limitation,
        )
    elif args.command == "prepare-review":
        result = probe.prepare_review(
            repo_root=root, artifact_path=args.artifact, r1_form_path=args.r1_form,
            r2_form_path=args.r2_form, fetch_manifest_path=args.fetch_manifest,
            fetch_report_path=args.fetch_report,
        )
        output = {"run_id": result.run_id, "status": "review_ready",
                  "forms": [str(path) for path in result.form_paths]}
    elif args.command == "lock-review":
        path = probe.lock_review(
            repo_root=root, artifact_path=args.artifact, lane=args.lane,
            completed_form_path=args.completed_form, locked_output_path=args.locked_output,
        )
        output = {"lane": args.lane, "locked": str(path)}
    elif args.command == "prepare-adjudication":
        packet, form = probe.prepare_adjudication(
            repo_root=root, artifact_path=args.artifact, packet_path=args.packet,
            adjudication_form_path=args.adjudication_form,
        )
        output = {"packet": str(packet), "form": str(form)}
    elif args.command == "lock-adjudication":
        path = probe.lock_adjudication(
            repo_root=root, artifact_path=args.artifact,
            completed_form_path=args.completed_form, locked_output_path=args.locked_output,
        )
        output = {"status": "adjudication_locked", "locked": str(path)}
    else:
        result = probe.reveal_score(repo_root=root, artifact_path=args.artifact,
                                    report_path=args.report, manifest_path=args.manifest)
        output = {"run_id": result.run_id, "status": result.status,
                  "report": str(result.report_path), "manifest": str(result.manifest_path)}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
