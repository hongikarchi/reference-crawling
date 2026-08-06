#!/usr/bin/env python3
"""Run the immutable Divisare Vision 1024/2048 benchmark sidecar."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_benchmark import run_benchmark  # noqa: E402
from canonical.divisare_vision_runtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
)


DEFAULT_SOURCE = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"
MIN_CODEX_CLI = (0, 146, 0)


def discover_codex_bin() -> Path:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("CODEX_BIN does not exist: %s" % path)
        return path
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable; pass --codex-bin")
    root = Path(local) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(
        root.glob("*/codex.exe"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("Codex desktop CLI was not found; pass --codex-bin")
    return candidates[0].resolve()


def codex_version(codex_bin: Path) -> str:
    process = subprocess.run(
        [str(codex_bin), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if process.returncode:
        raise RuntimeError("codex --version failed: %s" % (process.stderr or process.stdout))
    value = process.stdout.strip()
    if not value:
        raise RuntimeError("codex --version returned no version")
    return value


def require_supported_cli(version: str) -> None:
    match = re.search(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise RuntimeError("could not parse Codex CLI version: %s" % version)
    parsed = tuple(int(value) for value in match.groups())
    if parsed < MIN_CODEX_CLI:
        required = ".".join(str(value) for value in MIN_CODEX_CLI)
        raise RuntimeError(
            "Codex CLI %s is too old for gpt-5.6-sol; require >=%s. "
            "Update Codex or pass --codex-bin/CODEX_BIN."
            % (".".join(str(value) for value in parsed), required)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Divisare image semantics at long-edge 1024 and 2048."
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    codex_bin = (args.codex_bin or discover_codex_bin()).resolve()
    cli_version = codex_version(codex_bin)
    require_supported_cli(cli_version)
    result = run_benchmark(
        source_db=args.source_db,
        output_db=args.output_db,
        report_path=args.report,
        limit=args.limit,
        batch_size=args.batch_size,
        codex_bin=codex_bin,
        model=args.model,
        reasoning=args.reasoning,
        service_tier=args.service_tier,
        cli_version=cli_version,
        resume=args.resume,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
