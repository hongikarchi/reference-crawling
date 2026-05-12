#!/usr/bin/env python3
"""Create or update a DB Ops run record."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / ".claude" / "ops" / "runs"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "run"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create .claude/ops run record")
    parser.add_argument("slug", help="short run slug")
    parser.add_argument("--stage", default="unknown", help="stage name")
    parser.add_argument("--cmd", required=True, help="exact command launched or planned")
    parser.add_argument("--pid", default="pending", help="process id")
    parser.add_argument("--log", default="not specified", help="log file path")
    parser.add_argument("--output", default="not specified", help="primary output artifact")
    parser.add_argument("--status", default="running", help="running/done/failed/planned")
    args = parser.parse_args()

    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(args.slug)
    path = RUNS / f"{stamp}-{slug}.md"

    path.write_text(
        f"""# Run: {slug}

created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
stage: {args.stage}
status: {args.status}
pid: {args.pid}
log: {args.log}
output: {args.output}

## Command

```bash
{args.cmd}
```

## Monitor

- latest count:
- latest error:
- ETA:
- next check:

## Completion

- exit status:
- output count:
- structural validation:
- handoff:
""",
        encoding="utf-8",
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

