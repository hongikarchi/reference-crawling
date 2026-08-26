#!/usr/bin/env python3
"""CLI for WP2.5 immutable band-precision measurement."""

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

from canonical import vnext_identity_band_precision as precision


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
    result = argparse.ArgumentParser(description="Freeze and review WP2.5 band precision without mutation.")
    result.add_argument("--repo-root", type=_path, default=Path("."))
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="network-0 S1--S5 sample freeze")
    build.add_argument("--v4", type=_path, default=precision.DEFAULT_V4)
    build.add_argument("--output", type=_path, default=precision.DEFAULT_OUTPUT)
    build.add_argument("--sample-manifest", type=_path, default=precision.DEFAULT_SAMPLE_MANIFEST)
    build.add_argument("--protocol", type=_path, default=precision.DEFAULT_PROTOCOL)
    build.add_argument("--contract", type=_path, default=precision.DEFAULT_CONTRACT)
    build.add_argument("--n100-protocol", type=_path, default=precision.DEFAULT_N100_PROTOCOL)
    build.add_argument("--order", type=_path, default=precision.DEFAULT_ORDER)
    build.add_argument("--quota-remaining-percent", type=float, required=True)
    build.add_argument("--quota-observed-at", required=True)
    build.add_argument("--quota-source", required=True)
    build.add_argument("--quota-account-index", type=int, required=True)
    build.add_argument("--quota-limitation", required=True)

    quota = commands.add_parser("quota-check", help="append the required pre-wave quota gate")
    quota.add_argument("--artifact", type=_path, default=precision.DEFAULT_OUTPUT)
    quota.add_argument("--phase", required=True)
    quota.add_argument("--remaining-percent", type=float, required=True)
    quota.add_argument("--source", required=True)
    quota.add_argument("--observed-at", required=True)
    quota.add_argument("--account-index", type=int, default=0)
    quota.add_argument("--limitation", default="")

    prepare = commands.add_parser("prepare", help="bounded HTTP/cache preparation and two blind lanes")
    prepare.add_argument("--artifact", type=_path, default=precision.DEFAULT_OUTPUT)
    prepare.add_argument("--output-cache", type=_path, default=precision.DEFAULT_OUTPUT_CACHE)
    prepare.add_argument("--sheets-root", type=_path, default=precision.DEFAULT_SHEETS_ROOT)
    prepare.add_argument("--r1-form", type=_path, default=precision.DEFAULT_R1_FORM)
    prepare.add_argument("--r2-form", type=_path, default=precision.DEFAULT_R2_FORM)
    prepare.add_argument("--fetch-manifest", type=_path, default=precision.DEFAULT_FETCH_MANIFEST)
    prepare.add_argument("--fetch-report", type=_path, default=precision.DEFAULT_FETCH_REPORT)
    prepare.add_argument("--workers", type=int, choices=range(1, precision.MAX_WORKERS + 1), default=4)

    lock = commands.add_parser("lock-review", help="freeze one complete 221-row blind ledger")
    lock.add_argument("--artifact", type=_path, default=precision.DEFAULT_OUTPUT)
    lock.add_argument("--lane", required=True, choices=("r1", "r2"))
    lock.add_argument("--completed-form", type=_path, required=True)
    lock.add_argument("--locked-output", type=_path, required=True)

    unblind = commands.add_parser("unblind", help="unblind only after both review locks")
    unblind.add_argument("--artifact", type=_path, default=precision.DEFAULT_OUTPUT)
    unblind.add_argument("--packet", type=_path, default=precision.DEFAULT_ADJUDICATION_PACKET)
    unblind.add_argument("--adjudication-form", type=_path, default=precision.DEFAULT_ADJUDICATION_FORM)

    finalize = commands.add_parser("finalize", help="freeze gold and compute predeclared grades")
    finalize.add_argument("--artifact", type=_path, default=precision.DEFAULT_OUTPUT)
    finalize.add_argument("--completed-adjudication", type=_path, required=True)
    finalize.add_argument("--locked-adjudication", type=_path, default=precision.DEFAULT_ADJUDICATION_LOCKED)
    finalize.add_argument("--report", type=_path, default=precision.DEFAULT_REPORT)
    finalize.add_argument("--manifest", type=_path, default=precision.DEFAULT_MANIFEST)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    root = arguments.repo_root
    try:
        if arguments.command == "build":
            output = precision.build_sample(repo_root=root, v4_path=arguments.v4,
                output_path=arguments.output, manifest_path=arguments.sample_manifest,
                protocol_path=arguments.protocol, contract_path=arguments.contract,
                n100_protocol_path=arguments.n100_protocol, order_path=arguments.order,
                quota_remaining_percent=arguments.quota_remaining_percent,
                quota_observed_at=arguments.quota_observed_at,
                quota_source=arguments.quota_source,
                quota_account_index=arguments.quota_account_index,
                quota_limitation=arguments.quota_limitation)
        elif arguments.command == "quota-check":
            output = precision.record_quota_observation(repo_root=root,
                artifact_path=arguments.artifact, phase=arguments.phase,
                remaining_percent=arguments.remaining_percent, source=arguments.source,
                observed_at=arguments.observed_at, account_index=arguments.account_index,
                limitation=arguments.limitation)
        elif arguments.command == "prepare":
            output = precision.prepare_review(repo_root=root, artifact_path=arguments.artifact,
                output_cache_path=arguments.output_cache, sheets_root=arguments.sheets_root,
                r1_form_path=arguments.r1_form, r2_form_path=arguments.r2_form,
                fetch_manifest_path=arguments.fetch_manifest,
                fetch_report_path=arguments.fetch_report, workers=arguments.workers)
        elif arguments.command == "lock-review":
            output = precision.lock_review(repo_root=root, artifact_path=arguments.artifact,
                lane=arguments.lane, completed_form_path=arguments.completed_form,
                locked_output_path=arguments.locked_output)
        elif arguments.command == "unblind":
            output = precision.unblind_reviews(repo_root=root, artifact_path=arguments.artifact,
                packet_path=arguments.packet, adjudication_form_path=arguments.adjudication_form)
        else:
            output = precision.finalize_review(repo_root=root, artifact_path=arguments.artifact,
                completed_adjudication_path=arguments.completed_adjudication,
                locked_adjudication_path=arguments.locked_adjudication,
                report_path=arguments.report, manifest_path=arguments.manifest)
    except precision.IdentityBandPrecisionError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "result": _jsonable(output)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
