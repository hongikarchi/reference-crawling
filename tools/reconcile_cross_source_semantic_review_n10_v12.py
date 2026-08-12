#!/usr/bin/env python3
"""Reconcile two independently frozen, pixel-only N10 v1.2 references.

This module intentionally has no SQLite dependency and no model-result input.
It authenticates both full-population ledgers, surfaces every material label
disagreement, and refuses a final freeze until each difference has an explicit
pixel-grounded resolution and the unresolved count is zero.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from canonical.cross_source_semantic_vision_v12 import UNCERTAIN_AXES  # noqa: E402
from tools.inspect_cross_source_semantic_vision_n10_v12 import (  # noqa: E402
    FIELD_VALUES,
    QA_FIELDS,
    _canonical_review_row,
    _load_json,
    _manifest_binding,
    _validate_completed_row,
    _validate_review_manifest,
    _write_no_clobber,
    validate_independent_frozen_ledger,
)
from tools.render_cross_source_semantic_review_sheets_n10_v12 import (  # noqa: E402
    PRODUCTION_PINS,
    ReviewPins,
    _require_pins,
)


RECONCILER_VERSION = "cross-source-semantic-vision-n10-v1.2-reconciler-v1.0.0"
RECONCILIATION_TEMPLATE_VERSION = (
    "cross-source-semantic-vision-n10-v1.2-reconciliation-template-v1.0.0"
)
RECONCILED_LEDGER_VERSION = (
    "cross-source-semantic-vision-n10-v1.2-reconciled-agent-reference-v1.0.0"
)
RECONCILIATION_TEMPLATE_DOMAIN = (
    "cross-source-semantic-vision-n10-v1.2-reconciliation-template-v1"
)
RECONCILED_LEDGER_DOMAIN = (
    "cross-source-semantic-vision-n10-v1.2-reconciled-frozen-ledger-v1"
)
RECONCILIATION_ROW_KEYS = frozenset({
    "disagreement_dimensions", "final", "inference_id",
    "pixel_grounded_resolutions", "resolved_dimensions", "reviewer_a",
    "reviewer_b", "unresolved_dimensions",
})
RECONCILIATION_DRAFT_KEYS = frozenset({
    "authoritative", "created_at", "disagreement_counts_by_dimension",
    "disagreement_row_count", "field_vocabulary", "independent_ledgers",
    "model_results_read", "reconciler_id",
    "reconciliation_completed_before_unblinding", "reconciler_version",
    "reference_type", "review_manifest", "rows", "semantic_labels_read",
    "status", "template_sha256", "template_version", "uncertainty_axes",
    "unresolved_dimension_count",
})
RECONCILED_LEDGER_KEYS = frozenset({
    "authoritative", "completed_at", "field_vocabulary", "independent_ledgers",
    "ledger_sha256", "ledger_version", "model_results_read", "reconciled_by",
    "reconciliation", "reconciliation_completed_before_unblinding",
    "reconciler_version", "reference_type", "review_manifest", "reviewer_ids",
    "rows", "semantic_labels_read", "status", "uncertainty_axes",
})
RESOLUTION_RECORD_KEYS = frozenset({
    "disagreement_dimensions", "inference_id", "pixel_grounded_resolutions",
    "resolved_dimensions", "unresolved_dimensions",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dimension_value(row: Mapping[str, Any], dimension: str) -> Any:
    kind, _, field = dimension.partition(".")
    if kind == "primary" and field in QA_FIELDS:
        return row["primary"][field]
    if kind == "acceptable" and field in QA_FIELDS:
        return tuple(
            value for value in FIELD_VALUES[field] if value in row["acceptable_values"][field]
        )
    if dimension == "uncertainty.expected_uncertain_axes":
        return tuple(axis for axis in UNCERTAIN_AXES if axis in row["expected_uncertain_axes"])
    if dimension == "resolution.resolution_insufficient":
        return bool(row["resolution_insufficient"])
    raise ValueError(f"unknown reconciliation dimension: {dimension}")


MATERIAL_DIMENSIONS = tuple(
    [f"primary.{field}" for field in QA_FIELDS]
    + [f"acceptable.{field}" for field in QA_FIELDS]
    + ["uncertainty.expected_uncertain_axes", "resolution.resolution_insufficient"]
)


def _disagreements(row_a: Mapping[str, Any], row_b: Mapping[str, Any]) -> list[str]:
    return [
        dimension
        for dimension in MATERIAL_DIMENSIONS
        if _dimension_value(row_a, dimension) != _dimension_value(row_b, dimension)
    ]


def _independent_input(
    path: Path, ledger: Mapping[str, Any], byte_sha: str
) -> dict[str, Any]:
    return {
        "byte_sha256": byte_sha,
        "ledger_sha256": ledger["ledger_sha256"],
        "path_name": path.name,
        "reviewer_id": ledger["reviewer_id"],
    }


def _load_two_independent(
    *,
    review_manifest_path: Path,
    ledger_a_path: Path,
    ledger_b_path: Path,
    pins: ReviewPins,
) -> tuple[dict[str, Any], str, str, dict[str, Any], str, dict[str, Any], str]:
    validated_a = validate_independent_frozen_ledger(
        review_manifest_path=review_manifest_path,
        ledger_path=ledger_a_path,
        pins=pins,
    )
    validated_b = validate_independent_frozen_ledger(
        review_manifest_path=review_manifest_path,
        ledger_path=ledger_b_path,
        pins=pins,
    )
    manifest_a, ledger_a, manifest_byte_a, manifest_self_a, ledger_byte_a = validated_a
    manifest_b, ledger_b, manifest_byte_b, manifest_self_b, ledger_byte_b = validated_b
    if (
        manifest_a != manifest_b
        or manifest_byte_a != manifest_byte_b
        or manifest_self_a != manifest_self_b
    ):
        raise ValueError("independent ledgers do not share the exact review manifest")
    if ledger_a["reviewer_id"] == ledger_b["reviewer_id"]:
        raise ValueError("two distinct reviewer IDs are required")
    if ledger_a["ledger_sha256"] == ledger_b["ledger_sha256"]:
        raise ValueError("two independently frozen ledger identities are required")
    return (
        manifest_a,
        manifest_byte_a,
        manifest_self_a,
        ledger_a,
        ledger_byte_a,
        ledger_b,
        ledger_byte_b,
    )


def _template_digest_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable, blank-edit view authenticated by template SHA."""

    view = copy.deepcopy(dict(payload))
    view.pop("template_sha256", None)
    view["reconciliation_completed_before_unblinding"] = False
    unresolved = 0
    for row in view.get("rows", []):
        dimensions = list(row["disagreement_dimensions"])
        row["final"] = copy.deepcopy(row["reviewer_a"]) if not dimensions else None
        row["pixel_grounded_resolutions"] = {
            dimension: "" for dimension in dimensions
        }
        row["resolved_dimensions"] = []
        row["unresolved_dimensions"] = dimensions
        unresolved += len(dimensions)
    view["unresolved_dimension_count"] = unresolved
    return view


def create_reconciliation_template(
    *,
    review_manifest_path: Path,
    ledger_a_path: Path,
    ledger_b_path: Path,
    output_path: Path,
    reconciler_id: str,
    created_at: str | None = None,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Expose all reference disagreements, without reading model output/DB."""

    pins = _require_pins(pins)
    if not reconciler_id.strip():
        raise ValueError("reconciler_id must be non-empty")
    (
        manifest,
        manifest_byte,
        manifest_self,
        ledger_a,
        ledger_byte_a,
        ledger_b,
        ledger_byte_b,
    ) = _load_two_independent(
        review_manifest_path=review_manifest_path.resolve(),
        ledger_a_path=ledger_a_path.resolve(),
        ledger_b_path=ledger_b_path.resolve(),
        pins=pins,
    )
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row_a, row_b in zip(ledger_a["rows"], ledger_b["rows"]):
        dimensions = _disagreements(row_a, row_b)
        counts.update(dimensions)
        rows.append(
            {
                "disagreement_dimensions": dimensions,
                "final": _canonical_review_row(row_a) if not dimensions else None,
                "inference_id": row_a["inference_id"],
                "pixel_grounded_resolutions": {
                    dimension: "" for dimension in dimensions
                },
                "resolved_dimensions": [],
                "reviewer_a": _canonical_review_row(row_a),
                "reviewer_b": _canonical_review_row(row_b),
                "unresolved_dimensions": list(dimensions),
            }
        )
    payload: dict[str, Any] = {
        "authoritative": False,
        "created_at": created_at or _utc_now(),
        "disagreement_counts_by_dimension": dict(sorted(counts.items())),
        "disagreement_row_count": sum(bool(row["disagreement_dimensions"]) for row in rows),
        "field_vocabulary": {field: list(values) for field, values in FIELD_VALUES.items()},
        "independent_ledgers": [
            _independent_input(ledger_a_path, ledger_a, ledger_byte_a),
            _independent_input(ledger_b_path, ledger_b, ledger_byte_b),
        ],
        "model_results_read": False,
        "reconciler_id": reconciler_id,
        "reconciliation_completed_before_unblinding": False,
        "reconciler_version": RECONCILER_VERSION,
        "reference_type": "reconciliation_draft",
        "review_manifest": _manifest_binding(manifest, manifest_byte, manifest_self),
        "rows": rows,
        "semantic_labels_read": False,
        "status": "draft",
        "template_version": RECONCILIATION_TEMPLATE_VERSION,
        "uncertainty_axes": list(UNCERTAIN_AXES),
        "unresolved_dimension_count": sum(len(row["unresolved_dimensions"]) for row in rows),
    }
    payload["template_sha256"] = canonical_sha256(
        {"domain": RECONCILIATION_TEMPLATE_DOMAIN, "template": _template_digest_view(payload)}
    )
    _write_no_clobber(output_path, payload)
    return payload


def _validate_reconciliation_draft_binding(
    *,
    draft: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_byte: str,
    manifest_self: str,
    ledger_a_path: Path,
    ledger_a: Mapping[str, Any],
    ledger_byte_a: str,
    ledger_b_path: Path,
    ledger_b: Mapping[str, Any],
    ledger_byte_b: str,
) -> list[str]:
    if set(draft) != RECONCILIATION_DRAFT_KEYS:
        raise ValueError("reconciliation draft field set mismatch")
    if draft.get("status") != "draft" or draft.get("reference_type") != "reconciliation_draft":
        raise ValueError("only a v1.2 reconciliation draft may be frozen")
    if draft.get("template_version") != RECONCILIATION_TEMPLATE_VERSION:
        raise ValueError("reconciliation template version mismatch")
    if draft.get("reconciler_version") != RECONCILER_VERSION:
        raise ValueError("reconciler version mismatch")
    stored = draft.get("template_sha256")
    if not isinstance(stored, str) or stored != canonical_sha256(
        {"domain": RECONCILIATION_TEMPLATE_DOMAIN, "template": _template_digest_view(draft)}
    ):
        raise ValueError("reconciliation template SHA mismatch")
    if draft.get("review_manifest") != _manifest_binding(manifest, manifest_byte, manifest_self):
        raise ValueError("reconciliation review-manifest binding mismatch")
    expected_ledgers = [
        _independent_input(ledger_a_path, ledger_a, ledger_byte_a),
        _independent_input(ledger_b_path, ledger_b, ledger_byte_b),
    ]
    if draft.get("independent_ledgers") != expected_ledgers:
        raise ValueError("reconciliation independent-ledger binding mismatch")
    ids = list(manifest["ordered_inference_ids"])
    rows = draft.get("rows")
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("reconciliation requires the exact full population")
    if [row.get("inference_id") for row in rows if isinstance(row, dict)] != ids:
        raise ValueError("reconciliation row order mismatch")
    if any(not isinstance(row, dict) or set(row) != RECONCILIATION_ROW_KEYS for row in rows):
        raise ValueError("reconciliation row field set mismatch")
    return ids


def freeze_reconciled_ledger(
    *,
    review_manifest_path: Path,
    ledger_a_path: Path,
    ledger_b_path: Path,
    draft_path: Path,
    output_path: Path,
    completed_at: str | None = None,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> dict[str, Any]:
    """Freeze only after every material disagreement has been resolved."""

    pins = _require_pins(pins)
    (
        manifest,
        manifest_byte,
        manifest_self,
        ledger_a,
        ledger_byte_a,
        ledger_b,
        ledger_byte_b,
    ) = _load_two_independent(
        review_manifest_path=review_manifest_path.resolve(),
        ledger_a_path=ledger_a_path.resolve(),
        ledger_b_path=ledger_b_path.resolve(),
        pins=pins,
    )
    draft, _, _ = _load_json(draft_path.resolve())
    ids = _validate_reconciliation_draft_binding(
        draft=draft,
        manifest=manifest,
        manifest_byte=manifest_byte,
        manifest_self=manifest_self,
        ledger_a_path=ledger_a_path,
        ledger_a=ledger_a,
        ledger_byte_a=ledger_byte_a,
        ledger_b_path=ledger_b_path,
        ledger_b=ledger_b,
        ledger_byte_b=ledger_byte_b,
    )
    if draft.get("semantic_labels_read") is not False or draft.get("model_results_read") is not False:
        raise ValueError("reconciliation must finish before semantic/model unblinding")
    if draft.get("reconciliation_completed_before_unblinding") is not True:
        raise ValueError("pre-unblinding reconciliation attestation is required")

    final_rows: list[dict[str, Any]] = []
    resolution_records: list[dict[str, Any]] = []
    total_disagreements = 0
    for expected_id, draft_row, row_a, row_b in zip(
        ids, draft["rows"], ledger_a["rows"], ledger_b["rows"]
    ):
        if not isinstance(draft_row, dict):
            raise ValueError(f"reconciliation row must be an object: {expected_id}")
        expected_dimensions = _disagreements(row_a, row_b)
        if draft_row.get("disagreement_dimensions") != expected_dimensions:
            raise ValueError(f"recomputed disagreements differ for {expected_id}")
        if draft_row.get("reviewer_a") != _canonical_review_row(row_a) or draft_row.get("reviewer_b") != _canonical_review_row(row_b):
            raise ValueError(f"independent row evidence changed for {expected_id}")
        resolved = draft_row.get("resolved_dimensions")
        unresolved = draft_row.get("unresolved_dimensions")
        if resolved != expected_dimensions or unresolved != []:
            raise ValueError(f"all disagreement dimensions must be resolved for {expected_id}")
        reasons = draft_row.get("pixel_grounded_resolutions")
        if not isinstance(reasons, dict) or set(reasons) != set(expected_dimensions):
            raise ValueError(f"each disagreement needs one pixel-grounded resolution for {expected_id}")
        if any(not isinstance(reasons[dimension], str) or len(reasons[dimension].strip()) < 12 for dimension in expected_dimensions):
            raise ValueError(f"pixel-grounded reconciliation reason is required for {expected_id}")
        final = draft_row.get("final")
        if not isinstance(final, dict):
            raise ValueError(f"final reconciled row is required for {expected_id}")
        _validate_completed_row(final, expected_id)
        final = _canonical_review_row(final)
        for dimension in MATERIAL_DIMENSIONS:
            if dimension not in expected_dimensions and (
                _dimension_value(final, dimension) != _dimension_value(row_a, dimension)
            ):
                raise ValueError(f"agreed dimension changed during reconciliation: {expected_id}.{dimension}")
        total_disagreements += len(expected_dimensions)
        final_rows.append(final)
        resolution_records.append(
            {
                "disagreement_dimensions": expected_dimensions,
                "inference_id": expected_id,
                "pixel_grounded_resolutions": {
                    dimension: reasons[dimension].strip()
                    for dimension in expected_dimensions
                },
                "resolved_dimensions": list(expected_dimensions),
                "unresolved_dimensions": [],
            }
        )
    if int(draft.get("unresolved_dimension_count", -1)) != 0:
        raise ValueError("draft unresolved_dimension_count must be zero before freeze")

    frozen: dict[str, Any] = {
        "authoritative": False,
        "completed_at": completed_at or _utc_now(),
        "field_vocabulary": {field: list(values) for field, values in FIELD_VALUES.items()},
        "independent_ledgers": copy.deepcopy(draft["independent_ledgers"]),
        "ledger_version": RECONCILED_LEDGER_VERSION,
        "model_results_read": False,
        "reconciled_by": draft["reconciler_id"],
        "reconciliation": {
            "disagreement_dimension_count": total_disagreements,
            "disagreement_row_count": sum(bool(row["disagreement_dimensions"]) for row in resolution_records),
            "records": resolution_records,
            "unresolved_dimension_count": 0,
        },
        "reconciliation_completed_before_unblinding": True,
        "reconciler_version": RECONCILER_VERSION,
        "reference_type": "reconciled_agent_reference",
        "review_manifest": copy.deepcopy(draft["review_manifest"]),
        "reviewer_ids": [ledger_a["reviewer_id"], ledger_b["reviewer_id"]],
        "rows": final_rows,
        "semantic_labels_read": False,
        "status": "frozen",
        "uncertainty_axes": list(UNCERTAIN_AXES),
    }
    frozen["ledger_sha256"] = canonical_sha256(
        {"domain": RECONCILED_LEDGER_DOMAIN, "ledger": frozen}
    )
    _write_no_clobber(output_path, frozen)
    return frozen


def validate_reconciled_frozen_ledger(
    *,
    review_manifest_path: Path,
    ledger_path: Path,
    pins: ReviewPins | None = PRODUCTION_PINS,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    """Authenticate a final reconciled ledger before semantic DB access."""

    pins = _require_pins(pins)
    manifest, manifest_byte, manifest_self = _validate_review_manifest(
        review_manifest_path.resolve(), pins=pins
    )
    ledger, raw, ledger_byte = _load_json(ledger_path.resolve())
    if raw != (canonical_json(ledger) + "\n").encode("utf-8"):
        raise ValueError("reconciled ledger is not canonical JSON plus LF")
    if ledger.get("status") != "frozen" or ledger.get("reference_type") != "reconciled_agent_reference":
        raise ValueError("inspector requires a frozen reconciled reference")
    if set(ledger) != RECONCILED_LEDGER_KEYS:
        raise ValueError("reconciled ledger field set mismatch")
    if ledger.get("ledger_version") != RECONCILED_LEDGER_VERSION:
        raise ValueError("reconciled ledger version mismatch")
    if ledger.get("authoritative") is not False:
        raise ValueError("reconciled agent reference cannot be authoritative")
    if ledger.get("field_vocabulary") != {
        field: list(values) for field, values in FIELD_VALUES.items()
    } or ledger.get("uncertainty_axes") != list(UNCERTAIN_AXES):
        raise ValueError("reconciled ledger vocabulary contract mismatch")
    stored = ledger.get("ledger_sha256")
    body = copy.deepcopy(ledger)
    body.pop("ledger_sha256", None)
    if not isinstance(stored, str) or stored != canonical_sha256(
        {"domain": RECONCILED_LEDGER_DOMAIN, "ledger": body}
    ):
        raise ValueError("reconciled ledger SHA mismatch")
    if ledger.get("review_manifest") != _manifest_binding(manifest, manifest_byte, manifest_self):
        raise ValueError("reconciled ledger manifest binding mismatch")
    independent = ledger.get("independent_ledgers")
    reviewer_ids = ledger.get("reviewer_ids")
    if not isinstance(independent, list) or len(independent) != 2:
        raise ValueError("exactly two independent ledger bindings are required")
    if not isinstance(reviewer_ids, list) or len(reviewer_ids) != 2 or len(set(reviewer_ids)) != 2:
        raise ValueError("exactly two distinct reviewer IDs are required")
    if [item.get("reviewer_id") for item in independent if isinstance(item, dict)] != reviewer_ids:
        raise ValueError("reviewer IDs differ from independent ledger bindings")
    for item in independent:
        if not isinstance(item, dict) or set(item) != {
            "byte_sha256", "ledger_sha256", "path_name", "reviewer_id"
        }:
            raise ValueError("independent ledger provenance is incomplete")
        if not all(
            isinstance(item[name], str) and len(item[name]) == 64
            for name in ("byte_sha256", "ledger_sha256")
        ):
            raise ValueError("independent ledger SHA provenance is invalid")
    if any(ledger.get(key) is not False for key in ("semantic_labels_read", "model_results_read")):
        raise ValueError("reconciled reference was unblinded before freeze")
    if ledger.get("reconciliation_completed_before_unblinding") is not True:
        raise ValueError("pre-unblinding reconciliation attestation is absent")
    reconciliation = ledger.get("reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("unresolved_dimension_count") != 0:
        raise ValueError("reconciled reference still has unresolved dimensions")
    ids = list(manifest["ordered_inference_ids"])
    rows = ledger.get("rows")
    records = reconciliation.get("records")
    if not isinstance(rows, list) or len(rows) != len(ids) or not isinstance(records, list) or len(records) != len(ids):
        raise ValueError("reconciled ledger requires exact full-population rows and records")
    if [row.get("inference_id") for row in rows if isinstance(row, dict)] != ids:
        raise ValueError("reconciled rows do not preserve blind order")
    if [row.get("inference_id") for row in records if isinstance(row, dict)] != ids:
        raise ValueError("reconciliation records do not preserve blind order")
    total = 0
    disagreement_rows = 0
    for expected_id, row, record in zip(ids, rows, records):
        if not isinstance(record, dict) or set(record) != RESOLUTION_RECORD_KEYS:
            raise ValueError(f"reconciliation record field set mismatch: {expected_id}")
        _validate_completed_row(row, expected_id)
        if _canonical_review_row(row) != row:
            raise ValueError(f"reconciled row is not canonical: {expected_id}")
        dimensions = record.get("disagreement_dimensions")
        if not isinstance(dimensions, list) or any(value not in MATERIAL_DIMENSIONS for value in dimensions):
            raise ValueError(f"invalid disagreement dimensions: {expected_id}")
        if record.get("resolved_dimensions") != dimensions or record.get("unresolved_dimensions") != []:
            raise ValueError(f"reconciliation is incomplete: {expected_id}")
        reasons = record.get("pixel_grounded_resolutions")
        if not isinstance(reasons, dict) or set(reasons) != set(dimensions):
            raise ValueError(f"each disagreement needs one pixel-grounded resolution: {expected_id}")
        if any(not isinstance(reasons[dimension], str) or len(reasons[dimension].strip()) < 12 for dimension in dimensions):
            raise ValueError(f"pixel-grounded resolution is absent: {expected_id}")
        total += len(dimensions)
        disagreement_rows += bool(dimensions)
    if reconciliation.get("disagreement_dimension_count") != total:
        raise ValueError("reconciliation disagreement dimension count mismatch")
    if reconciliation.get("disagreement_row_count") != disagreement_rows:
        raise ValueError("reconciliation disagreement row count mismatch")
    return manifest, ledger, manifest_byte, manifest_self, ledger_byte


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile two independently frozen semantic N10 v1.2 references."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template")
    template.add_argument("--review-manifest", type=Path, required=True)
    template.add_argument("--ledger-a", type=Path, required=True)
    template.add_argument("--ledger-b", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    template.add_argument("--reconciler-id", required=True)
    template.add_argument("--created-at")
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--review-manifest", type=Path, required=True)
    freeze.add_argument("--ledger-a", type=Path, required=True)
    freeze.add_argument("--ledger-b", type=Path, required=True)
    freeze.add_argument("--draft", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--completed-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        result = create_reconciliation_template(
            review_manifest_path=args.review_manifest,
            ledger_a_path=args.ledger_a,
            ledger_b_path=args.ledger_b,
            output_path=args.output,
            reconciler_id=args.reconciler_id,
            created_at=args.created_at,
        )
    else:
        result = freeze_reconciled_ledger(
            review_manifest_path=args.review_manifest,
            ledger_a_path=args.ledger_a,
            ledger_b_path=args.ledger_b,
            draft_path=args.draft,
            output_path=args.output,
            completed_at=args.completed_at,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
