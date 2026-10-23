#!/usr/bin/env python
"""Run the independent vNext S6-lite building_images projection validator."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_s6_lite_projection_validator import VALIDATOR_VERSION, validate_projection  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/reports/vnext_s6_lite_building_images_v1_manifest.json"))
    parser.add_argument("--output", type=Path, help="write the validation JSON here (no clobber)")
    parser.add_argument("--no-production-pins", action="store_true", help="fixtures: skip the production input pin check")
    args = parser.parse_args(argv)
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    if args.output and args.output.exists():
        parser.error(f"refusing to clobber existing report: {args.output}")
    report = validate_projection(manifest, repo_root=ROOT, production_pins=not args.no_production_pins)
    payload = asdict(report)
    payload.update({"passed": report.passed, "status": "PASS" if report.passed else "FAIL",
                    "failed_check_names": report.failed_check_names, "validator_version": VALIDATOR_VERSION,
                    "manifest_sha256": __import__("hashlib").sha256(manifest.read_bytes()).hexdigest()})
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered + "\n")
    print(f"S6-LITE PROJECTION VALIDATION: {payload['status']}  checks {sum(c.passed for c in report.checks)}/{len(report.checks)}")
    for c in report.checks:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}")
        if not c.passed:
            print("    expected=" + json.dumps(c.expected, ensure_ascii=False, default=str)[:1500])
            print("    actual=" + json.dumps(c.actual, ensure_ascii=False, default=str)[:1500])
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
