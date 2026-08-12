#!/usr/bin/env python
"""Merge disjoint source-blind N100 review chunks into the frozen-order draft."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_image_selection import canonical_json  # noqa: E402


QA_FIELDS = (
    "in_scope",
    "reject_reason",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
    "project_legibility",
)


def _load_object(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"not UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _write_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload) + "\n")


def _expand_row(row: Mapping[str, Any], *, path: Path) -> dict[str, Any]:
    """Accept the full ledger row or a compact blind-review transport row."""

    if "inference_id" in row:
        return dict(row)
    expected = {"a", "id", "ri", "u", "v", "why"}
    if set(row) != expected:
        raise ValueError(f"compact row fields mismatch in {path}: {sorted(row)}")
    values = row["v"]
    alternatives = row["a"]
    if not isinstance(values, list) or len(values) != len(QA_FIELDS):
        raise ValueError(f"compact primary values mismatch in {path}")
    if not isinstance(alternatives, dict) or any(
        field not in QA_FIELDS or not isinstance(extra, list)
        for field, extra in alternatives.items()
    ):
        raise ValueError(f"compact alternatives mismatch in {path}")
    primary = dict(zip(QA_FIELDS, values))
    acceptable: dict[str, list[Any]] = {}
    for field in QA_FIELDS:
        candidates = [primary[field], *alternatives.get(field, [])]
        acceptable[field] = []
        for candidate in candidates:
            if candidate not in acceptable[field]:
                acceptable[field].append(candidate)
    return {
        "acceptable_values": acceptable,
        "expected_uncertain_axes": row["u"],
        "inference_id": row["id"],
        "primary": primary,
        "reason": row["why"],
        "resolution_insufficient": row["ri"],
    }


def merge_chunks(
    *, draft_path: Path, chunk_paths: Sequence[Path], output_path: Path
) -> dict[str, Any]:
    """Merge complete, disjoint blind chunks without reading semantic labels."""

    if not chunk_paths:
        raise ValueError("at least one review chunk is required")
    draft, _ = _load_object(draft_path.resolve())
    if draft.get("status") != "draft":
        raise ValueError("review base must be a draft ledger")
    if draft.get("reference_type") != "agent_reference":
        raise ValueError("review base must be an agent_reference ledger")
    if draft.get("semantic_labels_read") is not False:
        raise ValueError("review base is not source-blind")
    expected_rows = draft.get("rows")
    if not isinstance(expected_rows, list):
        raise ValueError("review base rows must be a list")
    expected_ids = [row.get("inference_id") for row in expected_rows if isinstance(row, dict)]
    if len(expected_ids) != len(expected_rows) or any(not isinstance(value, str) for value in expected_ids):
        raise ValueError("review base inference IDs are malformed")
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("review base inference IDs are not unique")

    by_id: dict[str, dict[str, Any]] = {}
    ranges: list[tuple[int, int]] = []
    chunk_records: list[dict[str, Any]] = []
    for path in chunk_paths:
        chunk, chunk_sha = _load_object(path.resolve())
        if chunk.get("reviewer_id") != draft.get("reviewer_id"):
            raise ValueError(f"reviewer mismatch: {path}")
        if chunk.get("blind") is not True:
            raise ValueError(f"chunk lacks blind attestation: {path}")
        start = chunk.get("sheet_start")
        end = chunk.get("sheet_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ValueError(f"invalid sheet range: {path}")
        ranges.append((start, end))
        rows = chunk.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"chunk rows must be a non-empty list: {path}")
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise ValueError(f"chunk contains a malformed row: {path}")
            row = _expand_row(raw_row, path=path)
            if not isinstance(row.get("inference_id"), str):
                raise ValueError(f"chunk contains a malformed row: {path}")
            inference_id = row["inference_id"]
            if inference_id in by_id:
                raise ValueError(f"duplicate reviewed inference ID: {inference_id}")
            by_id[inference_id] = row
        chunk_records.append(
            {
                "byte_sha256": chunk_sha,
                "path": str(path.resolve()),
                "row_count": len(rows),
                "sheet_end": end,
                "sheet_start": start,
            }
        )
    ordered_ranges = sorted(ranges)
    for previous, current in zip(ordered_ranges, ordered_ranges[1:]):
        if current[0] <= previous[1]:
            raise ValueError(f"overlapping sheet ranges: {previous} and {current}")
    missing = [value for value in expected_ids if value not in by_id]
    unexpected = sorted(set(by_id) - set(expected_ids))
    if missing or unexpected:
        raise ValueError(
            f"review row accounting mismatch: missing={missing}, unexpected={unexpected}"
        )

    merged = dict(draft)
    merged["rows"] = [by_id[value] for value in expected_ids]
    merged["blind_review_completed_before_unblinding"] = True
    merged["semantic_labels_read"] = False
    # This provenance is label-free and remains bound by the frozen ledger SHA.
    merged["blind_chunk_inputs"] = sorted(
        chunk_records, key=lambda row: (row["sheet_start"], row["sheet_end"])
    )
    _write_no_clobber(output_path.resolve(), merged)
    return {
        "blind_chunk_inputs": merged["blind_chunk_inputs"],
        "output": str(output_path.resolve()),
        "row_count": len(expected_ids),
        "semantic_labels_read": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge disjoint blind N100 agent-reference chunks into draft order."
    )
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--chunk", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = merge_chunks(
            draft_path=args.draft, chunk_paths=args.chunk, output_path=args.output
        )
    except Exception as exc:
        print(
            json.dumps(
                {"error": str(exc), "error_type": type(exc).__name__, "status": "error"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
