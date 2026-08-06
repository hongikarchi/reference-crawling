#!/usr/bin/env python3
"""Combine complete blinded pixel-review annotations into a reviewed pool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.divisare_n100_review import (  # noqa: E402
    DRAFT_SCHEMA,
    build_export,
    manifest_items,
    reviewed_pool_sha256,
    validate_import,
    validate_decision,
    write_json_no_clobber,
)
from tools.render_divisare_vision_review_sheet import (  # noqa: E402
    blinded_order,
    load_review_manifest,
)


DEFAULT_REVIEWER = "codex-5.6-sol-blinded-pixel-panel-20260805"


def load_annotations(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"annotation file must contain a list: {path}")
        for index, row in enumerate(payload, 1):
            if not isinstance(row, dict):
                raise ValueError(f"annotation row must be an object: {path}:{index}")
            rows.append(dict(row))
    return rows


def build_reviewed_pool(
    *,
    manifest: dict[str, Any],
    annotations: list[dict[str, Any]],
    adjudications: list[dict[str, Any]] | None = None,
    reviewer: str,
) -> dict[str, Any]:
    if not isinstance(reviewer, str) or not reviewer.startswith("codex-"):
        raise ValueError("agent-panel reviewer must use an explicit codex- identifier")
    items = manifest_items(manifest)
    items_by_id = {str(item["candidate_id"]): item for item in items}
    expected_slots = {
        item.candidate_id: ((item.blinded_index - 1) // 25 + 1, item.blinded_index)
        for item in blinded_order(items, manifest["manifest_sha256"])
    }
    decisions: dict[str, dict[str, Any]] = {}
    page_slots: set[tuple[int, int]] = set()
    source_reviewers: set[str] = set()

    for raw in annotations:
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in items_by_id:
            raise ValueError(f"unknown candidate_id: {candidate_id!r}")
        if candidate_id in decisions:
            raise ValueError(f"duplicate candidate annotation: {candidate_id}")
        page = raw.get("page")
        blinded_index = raw.get("blinded_index")
        if not isinstance(page, int) or page < 1:
            raise ValueError(f"{candidate_id} has invalid page")
        if not isinstance(blinded_index, int) or blinded_index < 1:
            raise ValueError(f"{candidate_id} has invalid blinded_index")
        slot = (page, blinded_index)
        if slot in page_slots:
            raise ValueError(f"duplicate blinded page slot: {slot}")
        if slot != expected_slots[candidate_id]:
            raise ValueError(
                f"{candidate_id} blinded slot mismatch: "
                f"expected={expected_slots[candidate_id]} actual={slot}"
            )
        page_slots.add(slot)
        source_reviewer = raw.get("reviewer")
        if not isinstance(source_reviewer, str) or not source_reviewer.startswith(
            "codex-5.6-sol-pixel-review-"
        ):
            raise ValueError(f"{candidate_id} has invalid reviewer provenance")
        source_reviewers.add(source_reviewer)

        decision = validate_decision(raw, valid_ids=set(items_by_id))
        item = items_by_id[candidate_id]
        if item.get("probe_status") != "success" and decision["disposition"] != "exclude":
            raise ValueError(f"failed probe must be excluded: {candidate_id}")
        decisions[candidate_id] = decision

    expected_ids = set(items_by_id)
    actual_ids = set(decisions)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(
            f"annotation coverage mismatch: expected={len(expected_ids)} "
            f"actual={len(actual_ids)} missing={missing[:10]} extra={extra[:10]}"
        )

    adjudicated_ids: list[str] = []
    for raw in adjudications or []:
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in decisions:
            raise ValueError(f"adjudication has unknown candidate_id: {candidate_id!r}")
        if candidate_id in adjudicated_ids:
            raise ValueError(f"duplicate adjudication: {candidate_id}")
        expected_page, expected_index = expected_slots[candidate_id]
        if (raw.get("page"), raw.get("blinded_index")) != (
            expected_page,
            expected_index,
        ):
            raise ValueError(f"{candidate_id} adjudication blinded slot mismatch")
        source_reviewer = raw.get("reviewer")
        if not isinstance(source_reviewer, str) or not source_reviewer.startswith(
            "codex-5.6-sol-pixel-review-"
        ):
            raise ValueError(f"{candidate_id} has invalid adjudicator provenance")
        source_reviewers.add(source_reviewer)
        decision = validate_decision(raw, valid_ids=expected_ids)
        if (
            items_by_id[candidate_id].get("probe_status") != "success"
            and decision["disposition"] != "exclude"
        ):
            raise ValueError(f"failed probe must be excluded: {candidate_id}")
        decisions[candidate_id] = decision
        adjudicated_ids.append(candidate_id)

    draft = {
        "schema_version": DRAFT_SCHEMA,
        "manifest_sha256": manifest["manifest_sha256"],
        "reviewer": reviewer,
        "updated_at": None,
        "decisions": decisions,
    }
    export = build_export(manifest, draft)
    export["review_provenance"] = {
        "review_mode": "blinded_pixel_only_agent_panel",
        "independent_human_review": False,
        "source_reviewers": sorted(source_reviewers),
        "annotation_file_count": None,
        "adjudicated_candidate_ids": sorted(adjudicated_ids),
    }
    export["reviewed_pool_sha256"] = reviewed_pool_sha256(export)
    validate_import(export, manifest)
    return export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine complete blinded Divisare pixel reviews."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, nargs="+", required=True)
    parser.add_argument("--adjudications", type=Path, nargs="*", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args(argv)

    manifest = load_review_manifest(args.manifest)
    annotations = load_annotations(args.annotations)
    adjudications = load_annotations(args.adjudications)
    result = build_reviewed_pool(
        manifest=manifest,
        annotations=annotations,
        adjudications=adjudications,
        reviewer=args.reviewer,
    )
    result["review_provenance"]["annotation_file_count"] = len(args.annotations)
    result["review_provenance"]["adjudication_file_count"] = len(args.adjudications)
    result["reviewed_pool_sha256"] = reviewed_pool_sha256(result)
    validate_import(result, manifest)
    write_json_no_clobber(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "decided_count": result["decided_count"],
                "included_count": result["included_count"],
                "excluded_count": result["excluded_count"],
                "reviewed_pool_sha256": result["reviewed_pool_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
