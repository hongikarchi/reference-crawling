#!/usr/bin/env python3
"""CLI for the WP2.3 N100 immutable-input review sidecar."""

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

from canonical import vnext_identity_n100 as n100


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
    result = argparse.ArgumentParser(
        description="Freeze, review, and gate WP2.3 identity N100 without mutating v3."
    )
    result.add_argument("--repo-root", type=_path, default=Path("."))
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="network-0 A70+B40 sample freeze")
    build.add_argument("--v3", type=_path, default=n100.DEFAULT_V3)
    build.add_argument("--output", type=_path, default=n100.DEFAULT_OUTPUT)
    build.add_argument("--sample-manifest", type=_path, default=n100.DEFAULT_SAMPLE_MANIFEST)
    build.add_argument("--protocol", type=_path, default=n100.DEFAULT_PROTOCOL)
    build.add_argument("--order", type=_path, default=n100.DEFAULT_ORDER)
    build.add_argument("--seed", default=n100.SEED)

    prepare = commands.add_parser(
        "prepare", help="bounded HTTP/cache preparation and two blind 105-row lanes"
    )
    prepare.add_argument("--artifact", type=_path, default=n100.DEFAULT_OUTPUT)
    prepare.add_argument("--output-cache", type=_path, default=n100.DEFAULT_OUTPUT_CACHE)
    prepare.add_argument("--sheets-root", type=_path, default=n100.DEFAULT_SHEETS_ROOT)
    prepare.add_argument("--r1-form", type=_path, default=n100.DEFAULT_R1_FORM)
    prepare.add_argument("--r2-form", type=_path, default=n100.DEFAULT_R2_FORM)
    prepare.add_argument("--fetch-manifest", type=_path, default=n100.DEFAULT_FETCH_MANIFEST)
    prepare.add_argument("--fetch-report", type=_path, default=n100.DEFAULT_FETCH_REPORT)
    prepare.add_argument("--workers", type=int, choices=range(1, n100.MAX_WORKERS + 1), default=4)

    lock = commands.add_parser("lock-review", help="freeze one complete blind ledger")
    lock.add_argument("--artifact", type=_path, default=n100.DEFAULT_OUTPUT)
    lock.add_argument("--lane", required=True, choices=("r1", "r2"))
    lock.add_argument("--completed-form", type=_path, required=True)
    lock.add_argument("--locked-output", type=_path, required=True)

    unblind = commands.add_parser("unblind", help="unblind only after both locks")
    unblind.add_argument("--artifact", type=_path, default=n100.DEFAULT_OUTPUT)
    unblind.add_argument("--packet", type=_path, default=n100.DEFAULT_ADJUDICATION_PACKET)
    unblind.add_argument("--adjudication-form", type=_path, default=n100.DEFAULT_ADJUDICATION_FORM)

    finalize = commands.add_parser("finalize", help="freeze gold and evaluate A70 gate")
    finalize.add_argument("--artifact", type=_path, default=n100.DEFAULT_OUTPUT)
    finalize.add_argument("--completed-adjudication", type=_path, required=True)
    finalize.add_argument("--locked-adjudication", type=_path, default=n100.DEFAULT_ADJUDICATION_LOCKED)
    finalize.add_argument("--gate", type=_path, default=n100.DEFAULT_GATE)
    finalize.add_argument("--report", type=_path, default=n100.DEFAULT_REPORT)
    finalize.add_argument("--manifest", type=_path, default=n100.DEFAULT_MANIFEST)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    root = arguments.repo_root
    try:
        if arguments.command == "build":
            output = n100.build_sample(
                repo_root=root,
                v3_path=arguments.v3,
                output_path=arguments.output,
                manifest_path=arguments.sample_manifest,
                protocol_path=arguments.protocol,
                order_path=arguments.order,
                seed=arguments.seed,
            )
        elif arguments.command == "prepare":
            output = n100.prepare_review(
                repo_root=root,
                artifact_path=arguments.artifact,
                output_cache_path=arguments.output_cache,
                sheets_root=arguments.sheets_root,
                r1_form_path=arguments.r1_form,
                r2_form_path=arguments.r2_form,
                fetch_manifest_path=arguments.fetch_manifest,
                fetch_report_path=arguments.fetch_report,
                workers=arguments.workers,
            )
        elif arguments.command == "lock-review":
            output = n100.lock_review(
                repo_root=root,
                artifact_path=arguments.artifact,
                lane=arguments.lane,
                completed_form_path=arguments.completed_form,
                locked_output_path=arguments.locked_output,
            )
        elif arguments.command == "unblind":
            output = n100.unblind_reviews(
                repo_root=root,
                artifact_path=arguments.artifact,
                packet_path=arguments.packet,
                adjudication_form_path=arguments.adjudication_form,
            )
        else:
            output = n100.finalize_review(
                repo_root=root,
                artifact_path=arguments.artifact,
                completed_adjudication_path=arguments.completed_adjudication,
                locked_adjudication_path=arguments.locked_adjudication,
                gate_path=arguments.gate,
                report_path=arguments.report,
                manifest_path=arguments.manifest,
            )
    except n100.IdentityN100Error as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "result": _jsonable(output)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
