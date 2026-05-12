#!/usr/bin/env python3
"""Create a compact Claude Gate review packet."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
REVIEWS = ROOT / ".claude" / "ops" / "reviews"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "review"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Claude Gate review packet")
    parser.add_argument("slug", help="short review slug")
    parser.add_argument("--stage", required=True, help="stage name")
    parser.add_argument("--question", required=True, help="one clear review question")
    parser.add_argument("--artifact", default="not specified", help="artifact to review")
    parser.add_argument("--sample", default="not specified", help="sample file/path or row ids")
    parser.add_argument("--risks", default="not specified", help="known risks")
    args = parser.parse_args()

    REVIEWS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(args.slug)
    path = REVIEWS / f"{stamp}-{slug}.md"

    path.write_text(
        f"""# Claude Gate Review: {slug}

created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
stage: {args.stage}

## Question

{args.question}

## Inputs

- artifact: {args.artifact}
- sample: {args.sample}
- known risks: {args.risks}

## Review Instructions

Read only the packet and the referenced sample/artifact slices needed to
answer the question. Do not review full logs or broad datasets unless this
packet explicitly asks for that scope.

Return exactly one verdict line:

```text
CLAUDE-GATE-PASS: {args.stage} <one-line reason>
CLAUDE-GATE-WARN: {args.stage} <row_ids> <one-line reason>
CLAUDE-GATE-BLOCK: {args.stage} <row_ids> <one-line reason>
```

If BLOCK, add at most five short bullets:

- failing rows:
- likely root cause:
- owner to fix:
- suggested narrow rerun:
- whether user approval is required:

## Verdict

pending
""",
        encoding="utf-8",
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

