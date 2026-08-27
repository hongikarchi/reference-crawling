#!/usr/bin/env python3
"""CLI for WP2.6 review-instrument calibration."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical import vnext_identity_instrument_probe as probe


def _path(value: str) -> Path:
    return Path(value)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Execute the WP2.6 calibration probe.")
    result.add_argument("--repo-root", type=_path, default=Path("."))
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Step 0 + Step 1 network-0 freeze")
    build.add_argument("--v4", type=_path, default=probe.DEFAULT_V4)
    build.add_argument("--n100", type=_path, default=probe.DEFAULT_N100)
    build.add_argument("--n100-manifest", type=_path, default=probe.DEFAULT_N100_MANIFEST)
    build.add_argument("--n100-locked", type=_path, default=probe.DEFAULT_N100_LOCKED)
    build.add_argument("--wp25", type=_path, default=probe.DEFAULT_WP25)
    build.add_argument("--wp25-manifest", type=_path, default=probe.DEFAULT_WP25_MANIFEST)
    build.add_argument("--protocol", type=_path, default=probe.DEFAULT_PROTOCOL)
    build.add_argument("--order", type=_path, default=probe.DEFAULT_ORDER)
    build.add_argument("--wp25-producer", type=_path, default=probe.DEFAULT_WP25_PRODUCER)
    build.add_argument("--wp25-protocol", type=_path, default=probe.DEFAULT_WP25_PROTOCOL)
    build.add_argument("--output", type=_path, default=probe.DEFAULT_OUTPUT)
    build.add_argument("--sample-manifest", type=_path, default=probe.DEFAULT_SAMPLE_MANIFEST)
    build.add_argument("--quota-remaining-percent", type=float, required=True)
    build.add_argument("--quota-observed-at", required=True)
    build.add_argument("--quota-source", required=True)
    build.add_argument("--quota-account-index", type=int, required=True)
    build.add_argument("--quota-limitation", required=True)

    declare = commands.add_parser("declare-gate", help="Step 2 immutable threshold declaration")
    declare.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    declare.add_argument("--sample-manifest", type=_path, default=probe.DEFAULT_SAMPLE_MANIFEST)

    quota = commands.add_parser("quota-check", help="append the next ordered quota gate")
    quota.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    quota.add_argument("--phase", required=True, choices=probe.QUOTA_PHASES[1:])
    quota.add_argument("--remaining-percent", type=float, required=True)
    quota.add_argument("--source", required=True)
    quota.add_argument("--observed-at", required=True)
    quota.add_argument("--account-index", type=int, default=0)
    quota.add_argument("--limitation", default="")

    prepare = commands.add_parser("prepare", help="reused WP2.5 bounded HTTP/blind packet")
    prepare.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    prepare.add_argument("--output-cache", type=_path, default=probe.DEFAULT_OUTPUT_CACHE)
    prepare.add_argument("--sheets-root", type=_path, default=probe.DEFAULT_SHEETS_ROOT)
    prepare.add_argument("--r1-form", type=_path, default=probe.DEFAULT_R1_FORM)
    prepare.add_argument("--r2-form", type=_path, default=probe.DEFAULT_R2_FORM)
    prepare.add_argument("--fetch-manifest", type=_path, default=probe.DEFAULT_FETCH_MANIFEST)
    prepare.add_argument("--fetch-report", type=_path, default=probe.DEFAULT_FETCH_REPORT)
    prepare.add_argument("--workers", type=int, choices=range(1, probe.MAX_WORKERS + 1), default=4)

    correction = commands.add_parser(
        "write-fetch-cap-correction",
        help="publish the no-clobber terminal fetch-cap correction overlay",
    )
    correction.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    correction.add_argument(
        "--fetch-manifest", type=_path, default=probe.DEFAULT_FETCH_MANIFEST)
    correction.add_argument("--report", type=_path, default=probe.DEFAULT_REPORT)
    correction.add_argument("--manifest", type=_path, default=probe.DEFAULT_MANIFEST)
    correction.add_argument(
        "--output", type=_path, default=probe.DEFAULT_FETCH_CAP_CORRECTION)

    lock = commands.add_parser("lock-review", help="reused WP2.5 reviewer lock")
    lock.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    lock.add_argument("--lane", required=True, choices=("r1", "r2"))
    lock.add_argument("--completed-form", type=_path, required=True)
    lock.add_argument("--locked-output", type=_path, required=True)

    adjudication = commands.add_parser("prepare-adjudication", help="blind third-actor packet")
    adjudication.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    adjudication.add_argument("--packet", type=_path, default=probe.DEFAULT_ADJUDICATION_PACKET)
    adjudication.add_argument("--adjudication-form", type=_path, default=probe.DEFAULT_ADJUDICATION_FORM)

    lock_adjudication = commands.add_parser("lock-adjudication", help="freeze third-actor ledger")
    lock_adjudication.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    lock_adjudication.add_argument("--completed-form", type=_path, required=True)
    lock_adjudication.add_argument("--locked-output", type=_path, default=probe.DEFAULT_ADJUDICATION_LOCKED)

    reveal = commands.add_parser("reveal-score", help="open N100 gold after all locks and score")
    reveal.add_argument("--artifact", type=_path, default=probe.DEFAULT_OUTPUT)
    reveal.add_argument("--report", type=_path, default=probe.DEFAULT_REPORT)
    reveal.add_argument("--manifest", type=_path, default=probe.DEFAULT_MANIFEST)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv); root = args.repo_root
    try:
        if args.command == "build":
            output = probe.build_sample(
                repo_root=root, v4_path=args.v4, n100_path=args.n100,
                n100_manifest_path=args.n100_manifest, n100_locked_path=args.n100_locked,
                wp25_path=args.wp25, wp25_manifest_path=args.wp25_manifest,
                protocol_path=args.protocol, order_path=args.order,
                wp25_producer_path=args.wp25_producer,
                wp25_protocol_path=args.wp25_protocol, output_path=args.output,
                manifest_path=args.sample_manifest,
                quota_remaining_percent=args.quota_remaining_percent,
                quota_observed_at=args.quota_observed_at, quota_source=args.quota_source,
                quota_account_index=args.quota_account_index,
                quota_limitation=args.quota_limitation)
        elif args.command == "declare-gate":
            output = probe.declare_gate(repo_root=root, artifact_path=args.artifact,
                                        sample_manifest_path=args.sample_manifest)
        elif args.command == "quota-check":
            output = probe.record_quota_observation(
                repo_root=root, artifact_path=args.artifact, phase=args.phase,
                remaining_percent=args.remaining_percent, source=args.source,
                observed_at=args.observed_at, account_index=args.account_index,
                limitation=args.limitation)
        elif args.command == "prepare":
            output = probe.prepare_review(
                repo_root=root, artifact_path=args.artifact,
                output_cache_path=args.output_cache, sheets_root=args.sheets_root,
                r1_form_path=args.r1_form, r2_form_path=args.r2_form,
                fetch_manifest_path=args.fetch_manifest,
                fetch_report_path=args.fetch_report, workers=args.workers)
        elif args.command == "write-fetch-cap-correction":
            output = probe.create_fetch_cap_correction(
                repo_root=root, artifact_path=args.artifact,
                fetch_manifest_path=args.fetch_manifest,
                report_path=args.report, manifest_path=args.manifest,
                output_path=args.output)
        elif args.command == "lock-review":
            output = probe.lock_review(
                repo_root=root, artifact_path=args.artifact, lane=args.lane,
                completed_form_path=args.completed_form,
                locked_output_path=args.locked_output)
        elif args.command == "prepare-adjudication":
            output = probe.prepare_adjudication(
                repo_root=root, artifact_path=args.artifact, packet_path=args.packet,
                adjudication_form_path=args.adjudication_form)
        elif args.command == "lock-adjudication":
            output = probe.lock_adjudication(
                repo_root=root, artifact_path=args.artifact,
                completed_form_path=args.completed_form,
                locked_output_path=args.locked_output)
        elif args.command == "reveal-score":
            output = probe.reveal_score(
                repo_root=root, artifact_path=args.artifact,
                report_path=args.report, manifest_path=args.manifest)
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(f"unhandled command: {args.command}")
    except probe.ERROR_BASE as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "result": _jsonable(output)},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
