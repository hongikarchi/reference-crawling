#!/usr/bin/env python
"""Run the independent WP1.1 drift-overlay validator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_drift_overlay_validator import (  # noqa: E402
    DEFAULT_ARTIFACT,
    validate_overlay,
)


DEFAULT_OUTPUT = Path("data/reports/vnext_gallery_draft_n10_v1_1_validation.json")


def _write_no_clobber(path: Path, payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"refusing to clobber validation artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise RuntimeError(f"stale validation temporary artifact: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently validate the WP1.1 N10 drift overlay.")
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-pending-reports", action="store_true")
    args = parser.parse_args(argv)
    report = validate_overlay(args.artifact, repo_root=ROOT, require_reports=not args.allow_pending_reports)
    payload = report.as_dict()
    target = (ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    _write_no_clobber(target, payload)
    print(
        json.dumps(
            {
                "passed": report.passed, "overlay_run_id": report.overlay_run_id,
                "status": report.status, "check_count": len(report.checks),
                "failed_checks": [row.name for row in report.checks if not row.passed],
                "metrics": dict(report.metrics), "output_path": str(target),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
