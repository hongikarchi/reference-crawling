#!/usr/bin/env python3
"""Finalize the blinded, double-reviewed Divisare axes development gold."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_review import (  # noqa: E402
    finalize_axes_gold_files,
    seal_reviewer_annotation_file,
    write_reviewer_annotation_template,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dev-manifest", type=Path, required=True)
    parser.add_argument(
        "--review-annotations",
        type=Path,
        nargs=2,
        metavar=("REVIEW_A", "REVIEW_B"),
    )
    parser.add_argument("--adjudication", type=Path)
    parser.add_argument("--write-review-template", action="store_true")
    parser.add_argument("--seal-review-template", type=Path)
    parser.add_argument("--reviewer-id")
    parser.add_argument("--review-context-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    mode_count = sum(
        (
            bool(args.review_annotations),
            args.write_review_template,
            args.seal_review_template is not None,
        )
    )
    if mode_count != 1:
        parser.error(
            "choose exactly one mode: --review-annotations, "
            "--write-review-template, or --seal-review-template"
        )
    if args.write_review_template:
        if not args.reviewer_id or not args.review_context_id:
            parser.error(
                "--write-review-template requires --reviewer-id and --review-context-id"
            )
        payload = write_reviewer_annotation_template(
            candidate_dev_manifest_path=args.candidate_dev_manifest,
            reviewer_id=args.reviewer_id,
            review_context_id=args.review_context_id,
            output_path=args.output,
        )
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "mode": "incomplete_review_template",
                    "annotation_count": len(payload["annotations"]),
                    "reviewer_id": payload["reviewer_id"],
                    "review_context_id": payload["review_context_id"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.seal_review_template is not None:
        payload = seal_reviewer_annotation_file(
            candidate_dev_manifest_path=args.candidate_dev_manifest,
            draft_path=args.seal_review_template,
            output_path=args.output,
        )
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "mode": "sealed_review_annotations",
                    "annotation_count": len(payload["annotations"]),
                    "logical_sha256": payload["logical_sha256"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    assert args.review_annotations is not None
    payload = finalize_axes_gold_files(
        candidate_dev_manifest_path=args.candidate_dev_manifest,
        reviewer_annotation_paths=args.review_annotations,
        adjudication_path=args.adjudication,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "sample_count": len(payload["samples"]),
                "development_only": payload["development_only"],
                "independent_human": payload["provenance"]["independent_human"],
                "logical_sha256": payload["logical_sha256"],
                "gold_manifest_sha256": payload["gold_manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
