#!/usr/bin/env python3
"""Operate the Architizer recrawl-v2 sidecar.

The ``full`` command is deliberately double-gated.  It is not valid without
the explicit confirmation flag, which should be supplied only after the user
has approved the N100-derived request/time/storage estimate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from crawl.architizer.recrawl_v2 import (  # noqa: E402
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_SOURCE_DB,
    DEFAULT_STATE_DB,
    inspect_sidecar_lock,
    preview_full_recrawl,
    recover_stale_sidecar_lock,
    render_network_report,
    run_award_seed_census,
    run_full_recrawl,
    run_network_smoke,
    write_text_no_clobber,
)


def _add_runtime_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR
    )


def _add_http_options(
    parser: argparse.ArgumentParser, *, default_delay: float = 2.0
) -> None:
    parser.add_argument("--delay", type=float, default=default_delay)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Architizer sidecar recrawl-v2 runner"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    awards = subparsers.add_parser(
        "awards",
        help="Discover official award-year tracks and direct entity seed links.",
    )
    _add_runtime_paths(awards)
    _add_http_options(awards)
    awards.add_argument("--year", type=int, required=True)

    for command, size in (("n10", 10), ("n100", 100)):
        smoke = subparsers.add_parser(
            command, help=f"Run the stratified N={size} network smoke."
        )
        _add_runtime_paths(smoke)
        _add_http_options(smoke)
        smoke.add_argument(
            "--report",
            type=Path,
            help="Optional new Markdown report; never overwrites an existing file.",
        )

    preview = subparsers.add_parser(
        "preview-full",
        help="Read-only full target/time/storage preview; performs no network fetch.",
    )
    preview.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    preview.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR
    )
    preview.add_argument("--delay", type=float, default=2.0)

    full = subparsers.add_parser(
        "full",
        help="Run full recrawl after explicit user approval.",
    )
    _add_runtime_paths(full)
    _add_http_options(full)
    full.add_argument(
        "--confirm-full-network-crawl",
        action="store_true",
        help="Required approval gate. Omit before the user explicitly approves.",
    )
    full.add_argument(
        "--report",
        type=Path,
        help="Optional new Markdown report; never overwrites an existing file.",
    )

    inspect_lock = subparsers.add_parser(
        "inspect-lock",
        help="Inspect sidecar lock metadata without changing it.",
    )
    inspect_lock.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)

    recover_lock = subparsers.add_parser(
        "recover-lock",
        help="Remove a proven-stale sidecar lock after explicit confirmation.",
    )
    recover_lock.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    recover_lock.add_argument("--minimum-age-seconds", type=float, default=300.0)
    recover_lock.add_argument(
        "--confirm-stale-lock-recovery",
        action="store_true",
        help="Required; live or unverifiable lock owners are never removed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "awards":
        result = run_award_seed_census(
            award_year=args.year,
            source_path=args.source_db,
            state_path=args.state_db,
            snapshot_root=args.snapshot_dir,
            delay_seconds=args.delay,
            timeout_seconds=args.timeout,
            max_attempts=args.max_attempts,
        )
    elif args.command in {"n10", "n100"}:
        size = 10 if args.command == "n10" else 100
        result = run_network_smoke(
            smoke_size=size,
            source_path=args.source_db,
            state_path=args.state_db,
            snapshot_root=args.snapshot_dir,
            delay_seconds=args.delay,
            timeout_seconds=args.timeout,
            max_attempts=args.max_attempts,
        )
        if args.report:
            write_text_no_clobber(
                args.report,
                render_network_report(
                    result,
                    f"Architizer source recrawl v2 N{size}",
                ),
            )
    elif args.command == "preview-full":
        result = preview_full_recrawl(
            state_path=args.state_db,
            snapshot_root=args.snapshot_dir,
            delay_seconds=args.delay,
        )
    elif args.command == "full":
        result = run_full_recrawl(
            confirmed=args.confirm_full_network_crawl,
            source_path=args.source_db,
            state_path=args.state_db,
            snapshot_root=args.snapshot_dir,
            delay_seconds=args.delay,
            timeout_seconds=args.timeout,
            max_attempts=args.max_attempts,
        )
        if args.report:
            write_text_no_clobber(
                args.report,
                render_network_report(result, "Architizer source recrawl v2 full"),
            )
    elif args.command == "inspect-lock":
        result = inspect_sidecar_lock(args.state_db)
    elif args.command == "recover-lock":
        result = recover_stale_sidecar_lock(
            args.state_db,
            confirmed=args.confirm_stale_lock_recovery,
            minimum_age_seconds=args.minimum_age_seconds,
        )
    else:  # pragma: no cover - argparse enforces choices
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
