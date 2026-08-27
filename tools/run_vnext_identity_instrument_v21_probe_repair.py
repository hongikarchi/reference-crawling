#!/usr/bin/env python3
"""Run the offline WP2.8-R packet-repair calibration workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical import vnext_identity_instrument_v21_probe_repair as repair


QUOTA_PHASES = (
    "pre_r1_review",
    "pre_r2_review",
    "pre_adjudication_review",
    "pre_reveal_score",
)


def _path(value: str) -> Path:
    return Path(value)


def _quota_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--remaining-percent", type=float, required=True)
    parser.add_argument("--observed-at", required=True, help="ISO-8601 with timezone")
    parser.add_argument("--quota-source", required=True)
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--limitation", default="")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact", type=_path, default=repair.DEFAULT_OUTPUT)
    parser.add_argument("--projection", type=_path, default=repair.DEFAULT_PROJECTION_DB)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=_path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser(
        "initialize", help="project the repaired packet and freeze the two gate contracts"
    )
    _quota_arguments(initialize)
    _common(initialize)
    initialize.add_argument(
        "--projection-manifest",
        type=_path,
        default=repair.DEFAULT_PROJECTION_MANIFEST,
    )
    initialize.add_argument(
        "--sample-manifest", type=_path, default=repair.DEFAULT_SAMPLE_MANIFEST
    )

    quota = commands.add_parser("record-quota", help="record the next actor-wave quota")
    _common(quota)
    quota.add_argument("--phase", choices=QUOTA_PHASES, required=True)
    _quota_arguments(quota)

    prepare = commands.add_parser("prepare-review", help="publish blind r1/r2 forms")
    _common(prepare)
    prepare.add_argument("--r1-form", type=_path, default=repair.DEFAULT_R1_FORM)
    prepare.add_argument("--r2-form", type=_path, default=repair.DEFAULT_R2_FORM)
    prepare.add_argument(
        "--fetch-manifest", type=_path, default=repair.DEFAULT_FETCH_MANIFEST
    )
    prepare.add_argument("--fetch-report", type=_path, default=repair.DEFAULT_FETCH_REPORT)

    lock = commands.add_parser("lock-review", help="lock one completed blind form")
    _common(lock)
    lock.add_argument("--lane", choices=("r1", "r2"), required=True)
    lock.add_argument("--completed-form", type=_path, required=True)
    lock.add_argument("--locked-output", type=_path, required=True)

    adjudicate = commands.add_parser(
        "prepare-adjudication", help="publish the blind reconciliation packet"
    )
    _common(adjudicate)
    adjudicate.add_argument(
        "--packet", type=_path, default=repair.DEFAULT_ADJUDICATION_PACKET
    )
    adjudicate.add_argument(
        "--adjudication-form", type=_path, default=repair.DEFAULT_ADJUDICATION_FORM
    )

    lock_adjudication = commands.add_parser(
        "lock-adjudication", help="lock the completed blind adjudication form"
    )
    _common(lock_adjudication)
    lock_adjudication.add_argument("--completed-form", type=_path, required=True)
    lock_adjudication.add_argument(
        "--locked-output", type=_path, default=repair.DEFAULT_ADJUDICATION_LOCKED
    )

    reveal = commands.add_parser(
        "reveal-score", help="perform the single control reveal and terminal score"
    )
    _common(reveal)
    reveal.add_argument("--core-report", type=_path, default=repair.DEFAULT_CORE_REPORT)
    reveal.add_argument(
        "--core-manifest", type=_path, default=repair.DEFAULT_CORE_MANIFEST
    )
    reveal.add_argument("--report", type=_path, default=repair.DEFAULT_REPORT)
    reveal.add_argument("--manifest", type=_path, default=repair.DEFAULT_MANIFEST)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root
    if args.command == "initialize":
        result = repair.initialize_repair(
            repo_root=root,
            quota_remaining_percent=args.remaining_percent,
            quota_observed_at=args.observed_at,
            quota_source=args.quota_source,
            quota_account_index=args.account_index,
            quota_limitation=args.limitation,
            projection_path=args.projection,
            projection_manifest_path=args.projection_manifest,
            artifact_path=args.artifact,
            sample_manifest_path=args.sample_manifest,
        )
        output = {
            "run_id": result.run_id,
            "status": result.status,
            "artifact": str(result.artifact_path),
            "projection": str(result.projection_path),
            "excluded_sample_ids": list(result.excluded_sample_ids),
        }
    elif args.command == "record-quota":
        output = repair.record_quota_observation(
            repo_root=root,
            phase=args.phase,
            remaining_percent=args.remaining_percent,
            source=args.quota_source,
            observed_at=args.observed_at,
            account_index=args.account_index,
            limitation=args.limitation,
            artifact_path=args.artifact,
            projection_path=args.projection,
        )
    elif args.command == "prepare-review":
        result = repair.prepare_review(
            repo_root=root,
            artifact_path=args.artifact,
            projection_path=args.projection,
            r1_form_path=args.r1_form,
            r2_form_path=args.r2_form,
            fetch_manifest_path=args.fetch_manifest,
            fetch_report_path=args.fetch_report,
        )
        output = {
            "run_id": result.run_id,
            "status": "review_ready",
            "forms": [str(path) for path in result.form_paths],
        }
    elif args.command == "lock-review":
        path = repair.lock_review(
            repo_root=root,
            lane=args.lane,
            completed_form_path=args.completed_form,
            locked_output_path=args.locked_output,
            artifact_path=args.artifact,
            projection_path=args.projection,
        )
        output = {"lane": args.lane, "locked": str(path)}
    elif args.command == "prepare-adjudication":
        packet, form = repair.prepare_adjudication(
            repo_root=root,
            artifact_path=args.artifact,
            projection_path=args.projection,
            packet_path=args.packet,
            adjudication_form_path=args.adjudication_form,
        )
        output = {"packet": str(packet), "form": str(form)}
    elif args.command == "lock-adjudication":
        path = repair.lock_adjudication(
            repo_root=root,
            completed_form_path=args.completed_form,
            locked_output_path=args.locked_output,
            artifact_path=args.artifact,
            projection_path=args.projection,
        )
        output = {"status": "adjudication_locked", "locked": str(path)}
    else:
        result = repair.reveal_score(
            repo_root=root,
            artifact_path=args.artifact,
            projection_path=args.projection,
            core_report_path=args.core_report,
            core_manifest_path=args.core_manifest,
            report_path=args.report,
            manifest_path=args.manifest,
        )
        output = {
            "run_id": result.run_id,
            "status": result.status,
            "report": str(result.report_path),
            "manifest": str(result.manifest_path),
        }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
