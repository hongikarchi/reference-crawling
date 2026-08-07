#!/usr/bin/env python
"""Independently validate one shared E1 fingerprint SQLite sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint_validator import (
    validate_image_fingerprint_sidecar,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute E1 source inventory, exclusion, source-record, and "
            "ordered-selection provenance without fetching images."
        )
    )
    parser.add_argument(
        "sidecar_path",
        nargs="?",
        type=Path,
        help="E1 sidecar SQLite path",
    )
    parser.add_argument(
        "source_db_path",
        nargs="?",
        type=Path,
        help="immutable source SQLite path",
    )
    parser.add_argument("--sidecar", dest="sidecar_flag", type=Path)
    parser.add_argument("--source-db", dest="source_db_flag", type=Path)
    parser.add_argument(
        "--recover",
        action="store_true",
        help=(
            "perform a normal writable SQLite recovery open/close before "
            "immutable validation; use only while holding the runner lock"
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    sidecar = args.sidecar_flag or args.sidecar_path
    source_db = args.source_db_flag or args.source_db_path
    if sidecar is None or source_db is None:
        parser.error("provide SIDECAR SOURCE_DB or --sidecar/--source-db")
    if args.sidecar_flag is not None and args.sidecar_path is not None:
        parser.error("sidecar path was provided twice")
    if args.source_db_flag is not None and args.source_db_path is not None:
        parser.error("source DB path was provided twice")
    try:
        result = validate_image_fingerprint_sidecar(
            sidecar,
            source_db,
            recover=args.recover,
        )
        payload = result.to_dict()
        print(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":") if args.compact else None,
                indent=None if args.compact else 2,
            )
        )
        return 0 if result.passed else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "passed": False,
                },
                sort_keys=True,
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
