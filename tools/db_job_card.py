#!/usr/bin/env python3
"""Create a DB Ops job card."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / ".claude" / "ops" / "jobs"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "job"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create .claude/ops job card")
    parser.add_argument("slug", help="short job slug, e.g. d1-resume")
    parser.add_argument("--owner", default="CONTROL", help="CONTROL/MATCHER/ENRICHER/ASSEMBLY/etc.")
    parser.add_argument("--stage", default="unknown", help="stage name, e.g. D-1")
    parser.add_argument("--write-scope", default="read-only until specified", help="allowed write scope")
    parser.add_argument("--input", default="not specified", help="input artifact")
    parser.add_argument("--output", default="not specified", help="output artifact")
    parser.add_argument("--claude-gate", action="store_true", help="mark Claude Gate review as required")
    args = parser.parse_args()

    JOBS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(args.slug)
    path = JOBS / f"{stamp}-{slug}.md"
    claude_gate = "required" if args.claude_gate else "not required unless risk changes"

    path.write_text(
        f"""# Job: {slug}

created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
owner: {args.owner}
stage: {args.stage}
status: draft

## Scope

write_scope: {args.write_scope}
input: {args.input}
output: {args.output}
claude_gate: {claude_gate}

## Goal

Describe the smallest useful outcome for this job before running it.

## Smoke Ladder

### N=10

- command:
- schema verdict:
- sample quality:
- tokens/cid:
- failure rate:
- decision:

### N=100

- command:
- schema verdict:
- sample quality:
- tokens/cid:
- projected full cost:
- failure rate:
- decision:

### Full

- approval:
- command:
- run record:
- monitor cadence:
- abort condition:

## Abort Conditions

- Schema mismatch.
- Unexpected writes outside write_scope.
- Cost projection exceeds approved budget.
- Failure rate or sample quality fails the stage-specific gate.
- User approval required but missing.

## Notes

- Keep logs in `logs/`; link paths here instead of pasting full logs.
- Add handoff lines to `.claude/Task.md` only for state transitions.
""",
        encoding="utf-8",
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

