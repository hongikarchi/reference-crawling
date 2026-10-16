#!/usr/bin/env python3
"""Freeze and inspect a blind reference ledger for semantic Vision N100.

The workflow intentionally has three separate phases:

``template`` creates a label-free, no-clobber review ledger from the blind
sheet manifest; ``freeze`` validates completed pixel-only judgments without
opening the semantic DB; and only ``inspect`` may join a cryptographically
frozen ledger to model results.  No phase performs network or model work.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.cross_source_image_selection import canonical_json, canonical_sha256  # noqa: E402
from canonical.cross_source_semantic_vision import (  # noqa: E402
    CAMERA_ANGLES,
    DRAWING_KINDS,
    FRAMING_SCALES,
    MEDIA,
    PROJECT_LEGIBILITY,
    PROJECT_STATES,
    REJECT_REASONS,
    SPATIAL_CONTEXTS,
    UNCERTAIN_AXES,
    derive_coverage_slots,
    derive_hero_decision,
    validate_invariants,
)
from tools.render_cross_source_semantic_review_sheets_n100 import (  # noqa: E402
    MANIFEST_DOMAIN as REVIEW_MANIFEST_DOMAIN,
    ORDER_DOMAIN as REVIEW_ORDER_DOMAIN,
)


INSPECTOR_VERSION = "cross-source-semantic-vision-n100-inspector-v1.0.0"
LEDGER_VERSION = "cross-source-semantic-vision-n100-agent-reference-v1.0.0"
LEDGER_TEMPLATE_DOMAIN = "cross-source-semantic-vision-n100-ledger-template-v1"
LEDGER_DOMAIN = "cross-source-semantic-vision-n100-frozen-ledger-v1"
INSPECTION_DOMAIN = "cross-source-semantic-vision-n100-inspection-v1"

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
FIELD_VALUES: dict[str, tuple[Any, ...]] = {
    "in_scope": (False, True),
    "reject_reason": tuple(REJECT_REASONS),
    "medium": tuple(MEDIA),
    "spatial_context": tuple(SPATIAL_CONTEXTS),
    "framing_scale": tuple(FRAMING_SCALES),
    "camera_angle": tuple(CAMERA_ANGLES),
    "drawing_kind": tuple(DRAWING_KINDS),
    "project_state": tuple(PROJECT_STATES),
    "project_legibility": tuple(PROJECT_LEGIBILITY),
}
UNCERTAINTY_FIELD = {
    "scope": "in_scope",
    "medium": "medium",
    "spatial_context": "spatial_context",
    "framing_scale": "framing_scale",
    "camera_angle": "camera_angle",
    "drawing_kind": "drawing_kind",
    "project_state": "project_state",
    "project_legibility": "project_legibility",
}
HERO_TIERS = (
    "preferred",
    "eligible",
    "fallback",
    "qa_only",
    "archive_only",
    "rejected",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_sha256(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload, raw, hashlib.sha256(raw).hexdigest()


def _write_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload) + "\n")


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _sqlite_sidecars(path: Path) -> list[str]:
    return [
        candidate.name
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    ]


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _validate_review_manifest(
    path: Path,
) -> tuple[dict[str, Any], str, str]:
    payload, raw, byte_sha = _load_json(path)
    stored = payload.get("manifest_sha256")
    if not isinstance(stored, str):
        raise ValueError("review manifest is missing manifest_sha256")
    without_self = copy.deepcopy(payload)
    without_self.pop("manifest_sha256", None)
    actual = canonical_sha256(
        {"domain": REVIEW_MANIFEST_DOMAIN, "manifest": without_self}
    )
    if stored != actual:
        raise ValueError(
            f"review manifest self SHA mismatch: expected={stored}, actual={actual}"
        )
    if payload.get("blind_labels_only") is not True:
        raise ValueError("review manifest is not source-blind")
    if payload.get("semantic_labels_exposed") is not False:
        raise ValueError("review manifest does not prove labels were hidden")
    if int(payload.get("images_per_sheet", 0)) != 6:
        raise ValueError("N100 review manifest must use six images per sheet")
    ids = payload.get("ordered_inference_ids")
    inputs = payload.get("inputs")
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        raise ValueError("review manifest ordered IDs are invalid")
    if len(ids) != len(set(ids)):
        raise ValueError("review manifest contains duplicate inference IDs")
    if not isinstance(inputs, list) or len(inputs) != len(ids):
        raise ValueError("review manifest input accounting mismatch")
    if [row.get("inference_id") for row in inputs if isinstance(row, dict)] != ids:
        raise ValueError("review manifest input order differs from ordered IDs")
    if int(payload.get("input_count", -1)) != len(ids):
        raise ValueError("review manifest input_count mismatch")
    expected_order_sha = canonical_sha256(
        {"domain": REVIEW_ORDER_DOMAIN, "ordered_inference_ids": ids}
    )
    if payload.get("ordered_inference_ids_sha256") != expected_order_sha:
        raise ValueError("review manifest ordered-ID SHA mismatch")
    if len(raw) == 0:
        raise ValueError("empty review manifest")
    return payload, byte_sha, stored


def _blank_review_row(inference_id: str) -> dict[str, Any]:
    return {
        "acceptable_values": {field: [] for field in QA_FIELDS},
        "expected_uncertain_axes": [],
        "inference_id": inference_id,
        "primary": {field: None for field in QA_FIELDS},
        "reason": "",
        "resolution_insufficient": None,
    }


def create_ledger_template(
    *,
    review_manifest_path: Path,
    output_path: Path,
    reviewer_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a label-free draft ledger without opening the semantic DB."""

    if not reviewer_id.strip():
        raise ValueError("reviewer_id must be non-empty")
    manifest, manifest_byte_sha, manifest_self_sha = _validate_review_manifest(
        review_manifest_path.resolve()
    )
    ids = list(manifest["ordered_inference_ids"])
    payload: dict[str, Any] = {
        "authoritative": False,
        "blind_review_completed_before_unblinding": False,
        "created_at": created_at or _utc_now(),
        "field_vocabulary": {
            field: list(values) for field, values in FIELD_VALUES.items()
        },
        "ledger_version": LEDGER_VERSION,
        "reference_type": "agent_reference",
        "review_manifest": {
            "byte_sha256": manifest_byte_sha,
            "input_count": len(ids),
            "manifest_sha256": manifest_self_sha,
            "ordered_inference_ids_sha256": manifest[
                "ordered_inference_ids_sha256"
            ],
            "semantic_db_byte_sha256": manifest["semantic_db"][
                "byte_sha256_after"
            ],
            "semantic_run_id": manifest["semantic_run"]["run_id"],
        },
        "reviewer_id": reviewer_id,
        "rows": [_blank_review_row(inference_id) for inference_id in ids],
        "semantic_labels_read": False,
        "status": "draft",
        "uncertainty_axes": list(UNCERTAIN_AXES),
    }
    payload["template_sha256"] = canonical_sha256(
        {"domain": LEDGER_TEMPLATE_DOMAIN, "template": payload}
    )
    _write_no_clobber(output_path, payload)
    return payload


def _as_ordered_unique_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if len(value) != len({json.dumps(item, sort_keys=True) for item in value}):
        raise ValueError(f"{label} contains duplicates")
    return value


def _validate_completed_review_row(row: Mapping[str, Any], expected_id: str) -> None:
    if row.get("inference_id") != expected_id:
        raise ValueError(
            f"review row ID mismatch: expected={expected_id}, actual={row.get('inference_id')}"
        )
    primary = row.get("primary")
    acceptable = row.get("acceptable_values")
    if not isinstance(primary, dict) or set(primary) != set(QA_FIELDS):
        raise ValueError(f"primary field set mismatch for {expected_id}")
    if not isinstance(acceptable, dict) or set(acceptable) != set(QA_FIELDS):
        raise ValueError(f"acceptable field set mismatch for {expected_id}")
    for field in QA_FIELDS:
        value = primary[field]
        if field == "in_scope" and not isinstance(value, bool):
            raise ValueError(f"in_scope must be boolean for {expected_id}")
        if value not in FIELD_VALUES[field]:
            raise ValueError(f"unsupported primary {field} for {expected_id}: {value!r}")
        values = _as_ordered_unique_list(
            acceptable[field], label=f"{expected_id}.{field}.acceptable_values"
        )
        if field == "in_scope" and any(not isinstance(item, bool) for item in values):
            raise ValueError(f"acceptable in_scope values must be boolean for {expected_id}")
        if not values or any(item not in FIELD_VALUES[field] for item in values):
            raise ValueError(f"invalid acceptable values for {expected_id}.{field}")
        if value not in values:
            raise ValueError(
                f"primary value must be acceptable for {expected_id}.{field}"
            )
    axes = _as_ordered_unique_list(
        row.get("expected_uncertain_axes"),
        label=f"{expected_id}.expected_uncertain_axes",
    )
    if any(axis not in UNCERTAIN_AXES for axis in axes):
        raise ValueError(f"unsupported uncertainty axis for {expected_id}")
    resolution = row.get("resolution_insufficient")
    if not isinstance(resolution, bool):
        raise ValueError(f"resolution_insufficient must be boolean for {expected_id}")
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"pixel-grounded reason is required for {expected_id}")
    semantic = {
        "asset_id": expected_id,
        **{field: primary[field] for field in QA_FIELDS},
        "uncertain_axes": tuple(axes),
        "resolution_insufficient": resolution,
        "evidence": reason.strip(),
    }
    validate_invariants(semantic)


def _validate_ledger_binding(
    ledger: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_byte_sha: str,
    manifest_self_sha: str,
) -> list[str]:
    binding = ledger.get("review_manifest")
    if not isinstance(binding, dict):
        raise ValueError("review ledger is missing review_manifest binding")
    expected = {
        "byte_sha256": manifest_byte_sha,
        "input_count": int(manifest["input_count"]),
        "manifest_sha256": manifest_self_sha,
        "ordered_inference_ids_sha256": manifest["ordered_inference_ids_sha256"],
        "semantic_db_byte_sha256": manifest["semantic_db"]["byte_sha256_after"],
        "semantic_run_id": manifest["semantic_run"]["run_id"],
    }
    if binding != expected:
        raise ValueError(
            f"review-ledger manifest binding mismatch: expected={expected}, actual={binding}"
        )
    ids = list(manifest["ordered_inference_ids"])
    rows = ledger.get("rows")
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("review-ledger row accounting mismatch")
    if [row.get("inference_id") for row in rows if isinstance(row, dict)] != ids:
        raise ValueError("review-ledger rows do not preserve blind order")
    return ids


def freeze_ledger(
    *,
    review_manifest_path: Path,
    draft_path: Path,
    output_path: Path,
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Freeze a complete blind ledger before any semantic-result join."""

    manifest, manifest_byte_sha, manifest_self_sha = _validate_review_manifest(
        review_manifest_path.resolve()
    )
    draft, _, _ = _load_json(draft_path.resolve())
    if draft.get("status") != "draft":
        raise ValueError("only a draft ledger may be frozen")
    if draft.get("reference_type") != "agent_reference":
        raise ValueError("this workflow records an agent reference, not human gold")
    if draft.get("semantic_labels_read") is not False:
        raise ValueError("ledger cannot be frozen after semantic labels were read")
    if draft.get("blind_review_completed_before_unblinding") is not True:
        raise ValueError("blind review completion must be attested before freeze")
    ids = _validate_ledger_binding(
        draft, manifest, manifest_byte_sha, manifest_self_sha
    )
    for expected_id, row in zip(ids, draft["rows"]):
        if not isinstance(row, dict):
            raise ValueError(f"review row must be an object: {expected_id}")
        _validate_completed_review_row(row, expected_id)

    frozen = copy.deepcopy(draft)
    frozen.pop("template_sha256", None)
    frozen["completed_at"] = completed_at or _utc_now()
    frozen["status"] = "frozen"
    frozen["ledger_sha256"] = canonical_sha256(
        {"domain": LEDGER_DOMAIN, "ledger": frozen}
    )
    _write_no_clobber(output_path, frozen)
    return frozen


def _validate_frozen_ledger(
    *,
    review_manifest_path: Path,
    ledger_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    manifest, manifest_byte_sha, manifest_self_sha = _validate_review_manifest(
        review_manifest_path.resolve()
    )
    ledger, _, _ = _load_json(ledger_path.resolve())
    if ledger.get("status") != "frozen":
        raise ValueError("semantic results may only be joined to a frozen ledger")
    stored = ledger.get("ledger_sha256")
    if not isinstance(stored, str):
        raise ValueError("frozen ledger is missing ledger_sha256")
    without_self = copy.deepcopy(ledger)
    without_self.pop("ledger_sha256", None)
    actual = canonical_sha256({"domain": LEDGER_DOMAIN, "ledger": without_self})
    if stored != actual:
        raise ValueError(f"frozen ledger SHA mismatch: expected={stored}, actual={actual}")
    ids = _validate_ledger_binding(
        ledger, manifest, manifest_byte_sha, manifest_self_sha
    )
    for expected_id, row in zip(ids, ledger["rows"]):
        _validate_completed_review_row(row, expected_id)
    return manifest, ledger, manifest_byte_sha, manifest_self_sha


def _gallery_depth_bucket(count: int) -> str:
    if count <= 3:
        return "1_3"
    if count <= 6:
        return "4_6"
    if count <= 12:
        return "7_12"
    if count <= 24:
        return "13_24"
    return "25_plus"


def _building_gallery_depth_bucket(manifest_json: str) -> str:
    payload = json.loads(manifest_json)
    selected = payload.get("selected_building", payload)
    building = selected.get("building", selected)
    value = building.get("successful_gallery_count")
    if isinstance(value, int) and value >= 0:
        return _gallery_depth_bucket(value)
    sampling = selected.get("sampling_stratum")
    if isinstance(sampling, str):
        match = re.search(r"gallery_(1_3|4_6|7_12|13_24|25_plus)$", sampling)
        if match:
            return match.group(1)
    raise ValueError(
        "selected-building manifest lacks successful_gallery_count or a gallery-depth stratum"
    )


def _origins(manifest_json: str) -> tuple[str, ...]:
    payload = json.loads(manifest_json)
    occurrence = payload.get("occurrence", payload)
    values = occurrence.get("origins")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("occurrence manifest lacks origins")
    return tuple(values)


def _semantic_from_reference(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": row["inference_id"],
        **{field: row["primary"][field] for field in QA_FIELDS},
        "uncertain_axes": tuple(row["expected_uncertain_axes"]),
        "resolution_insufficient": bool(row["resolution_insufficient"]),
        "evidence": str(row["reason"]),
    }


def _semantic_from_db(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": str(row["inference_id"]),
        "in_scope": bool(row["in_scope"]),
        **{field: str(row[field]) for field in QA_FIELDS if field != "in_scope"},
        "uncertain_axes": tuple(json.loads(str(row["uncertain_axes_json"]))),
        "resolution_insufficient": bool(row["resolution_insufficient"]),
        "evidence": str(row["evidence"]),
    }


def _confusion_records(counter: Counter[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [
        {"count": count, "model": model, "reference": reference}
        for (reference, model), count in sorted(
            counter.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
        )
    ]


def _uncertainty_metric(tp: int, fp: int, fn: int, tn: int) -> dict[str, Any]:
    return {
        "f1": (
            None
            if 2 * tp + fp + fn == 0
            else round((2 * tp) / (2 * tp + fp + fn), 6)
        ),
        "fn": fn,
        "fp": fp,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "tn": tn,
        "tp": tp,
    }


def _coverage_summary(
    *,
    buildings: Mapping[str, Mapping[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
    slots_by_id: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    by_building: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        by_building[str(occurrence["selection_id"])].append(occurrence)
    rows: list[dict[str, Any]] = []
    gained_slots: Counter[str] = Counter()
    total_p2_pairs = 0
    total_expanded_pairs = 0
    expanded_only_images = 0
    for selection_id, building in sorted(buildings.items()):
        p2_ids: set[str] = set()
        expanded_ids: set[str] = set()
        for occurrence in by_building.get(selection_id, []):
            inference_id = str(occurrence["inference_id"])
            expanded_ids.add(inference_id)
            origins = occurrence["origins"]
            if any(origin.startswith("representative_p2_rank_") for origin in origins):
                p2_ids.add(inference_id)
        p2_slots = sorted(
            {slot for inference_id in p2_ids for slot in slots_by_id.get(inference_id, ())}
        )
        expanded_slots = sorted(
            {
                slot
                for inference_id in expanded_ids
                for slot in slots_by_id.get(inference_id, ())
            }
        )
        new_slots = sorted(set(expanded_slots) - set(p2_slots))
        gained_slots.update(new_slots)
        total_p2_pairs += len(p2_slots)
        total_expanded_pairs += len(expanded_slots)
        expanded_only_images += len(expanded_ids - p2_ids)
        rows.append(
            {
                "expanded_image_count": len(expanded_ids),
                "expanded_slot_count": len(expanded_slots),
                "expanded_slots": expanded_slots,
                "gallery_depth_bucket": building["gallery_depth_bucket"],
                "new_slot_count": len(new_slots),
                "new_slots": new_slots,
                "p2_image_count": len(p2_ids),
                "p2_slot_count": len(p2_slots),
                "p2_slots": p2_slots,
                "population_stratum": building["population_stratum"],
                "selection_id": selection_id,
                "source": building["source"],
            }
        )
    return {
        "building_rows": rows,
        "buildings_with_new_slots": sum(row["new_slot_count"] > 0 for row in rows),
        "incremental_slot_yield_per_expanded_only_image": _ratio(
            total_expanded_pairs - total_p2_pairs, expanded_only_images
        ),
        "new_slot_building_distribution": dict(sorted(gained_slots.items())),
        "total_buildings": len(rows),
        "total_expanded_building_slot_pairs": total_expanded_pairs,
        "total_expanded_only_images": expanded_only_images,
        "total_new_building_slot_pairs": total_expanded_pairs - total_p2_pairs,
        "total_p2_building_slot_pairs": total_p2_pairs,
    }


def inspect_frozen_ledger(
    *,
    semantic_db: Path,
    review_manifest_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    """Join a frozen blind reference to N100 results using immutable SQLite."""

    # This validation deliberately happens before the semantic DB is opened.
    review_manifest, ledger, review_manifest_byte_sha, review_manifest_self_sha = (
        _validate_frozen_ledger(
            review_manifest_path=review_manifest_path, ledger_path=ledger_path
        )
    )
    semantic_db = semantic_db.resolve()
    if not semantic_db.is_file():
        raise FileNotFoundError(semantic_db)
    size_before = semantic_db.stat().st_size
    sha_before = _file_sha256(semantic_db)
    expected_db_sha = review_manifest["semantic_db"]["byte_sha256_after"]
    if sha_before != expected_db_sha:
        raise ValueError(
            f"semantic DB differs from blind-review manifest: expected={expected_db_sha}, actual={sha_before}"
        )
    sidecars_before = _sqlite_sidecars(semantic_db)

    connection = _open_immutable(semantic_db)
    try:
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError(f"semantic DB must contain one run, found {len(runs)}")
        run = dict(runs[0])
        if int(run["building_count"]) != 100:
            raise ValueError("semantic DB is not an N100 artifact")
        if str(run["run_id"]) != ledger["review_manifest"]["semantic_run_id"]:
            raise ValueError("semantic run ID differs from frozen ledger")

        building_rows = [dict(row) for row in connection.execute(
            """
            SELECT selection_id,source,population_stratum,qa_fallback,manifest_json
            FROM selected_buildings WHERE run_id=? ORDER BY building_rank
            """,
            (run["run_id"],),
        )]
        if len(building_rows) != 100:
            raise ValueError(f"selected building accounting mismatch: {len(building_rows)}")
        buildings: dict[str, dict[str, Any]] = {}
        for row in building_rows:
            buildings[str(row["selection_id"])] = {
                "gallery_depth_bucket": _building_gallery_depth_bucket(
                    str(row["manifest_json"])
                ),
                "population_stratum": str(row["population_stratum"]),
                "qa_fallback": bool(row["qa_fallback"]),
                "source": str(row["source"]),
            }

        occurrence_rows = [dict(row) for row in connection.execute(
            """
            SELECT input_rank,inference_id,selection_id,source,manifest_json
            FROM selected_occurrences WHERE run_id=? ORDER BY input_rank,inference_id
            """,
            (run["run_id"],),
        )]
        occurrences: list[dict[str, Any]] = []
        for row in occurrence_rows:
            building = buildings[str(row["selection_id"])]
            occurrences.append(
                {
                    **row,
                    "gallery_depth_bucket": building["gallery_depth_bucket"],
                    "origins": _origins(str(row["manifest_json"])),
                    "population_stratum": building["population_stratum"],
                }
            )
        if len(occurrences) != int(run["occurrence_count"]):
            raise ValueError("selected occurrence accounting mismatch")

        result_rows = [dict(row) for row in connection.execute(
            """
            SELECT inference_id,in_scope,reject_reason,medium,spatial_context,
                   framing_scale,camera_angle,drawing_kind,project_state,
                   project_legibility,uncertain_axes_json,
                   resolution_insufficient,evidence
            FROM semantic_results WHERE run_id=? ORDER BY inference_id
            """,
            (run["run_id"],),
        )]
        results = {str(row["inference_id"]): _semantic_from_db(row) for row in result_rows}
        links = {
            str(row["inference_id"]): str(row["result_inference_id"])
            for row in connection.execute(
                """
                SELECT inference_id,result_inference_id
                FROM occurrence_result_links WHERE run_id=?
                """,
                (run["run_id"],),
            )
        }
        stored_hero = {
            str(row["inference_id"]): str(row["tier"])
            for row in connection.execute(
                "SELECT inference_id,tier FROM hero_candidate_decisions WHERE run_id=?",
                (run["run_id"],),
            )
        }
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        runtime = {
            "fetch_attempts": int(connection.execute(
                "SELECT COUNT(*) FROM fetch_attempts WHERE run_id=?", (run["run_id"],)
            ).fetchone()[0]),
            "vision_attempts": int(connection.execute(
                "SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run["run_id"],)
            ).fetchone()[0]),
            "input_tokens": int(connection.execute(
                "SELECT COALESCE(SUM(input_tokens),0) FROM vision_attempts WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0]),
            "cached_input_tokens": int(connection.execute(
                "SELECT COALESCE(SUM(cached_input_tokens),0) FROM vision_attempts WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0]),
            "output_tokens": int(connection.execute(
                "SELECT COALESCE(SUM(output_tokens),0) FROM vision_attempts WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0]),
        }
    finally:
        connection.close()

    ledger_rows = {str(row["inference_id"]): row for row in ledger["rows"]}
    review_ids = set(review_manifest["ordered_inference_ids"])
    occurrence_ids = {str(row["inference_id"]) for row in occurrences}
    if set(ledger_rows) != review_ids:
        raise ValueError("frozen ledger and blind-review IDs differ")
    if not review_ids.issubset(occurrence_ids):
        raise ValueError("blind-review manifest contains unknown occurrence IDs")
    unrendered_occurrence_ids = sorted(occurrence_ids - review_ids)

    field_counts: dict[str, dict[str, Any]] = {
        field: {
            "acceptable": 0,
            "clear_errors": [],
            "confusion": Counter(),
            "strict": 0,
            "support": 0,
        }
        for field in QA_FIELDS
    }
    uncertainty_counts = {
        axis: {"fn_rows": [], "fp_rows": [], "fn": 0, "fp": 0, "tn": 0, "tp": 0}
        for axis in UNCERTAIN_AXES
    }
    resolution_counts = {"fn_rows": [], "fp_rows": [], "fn": 0, "fp": 0, "tn": 0, "tp": 0}
    model_slots: dict[str, tuple[str, ...]] = {}
    reference_slots: dict[str, tuple[str, ...]] = {}
    hero_confusion: Counter[tuple[str, str]] = Counter()
    hero_disagreements: list[dict[str, Any]] = []
    hero_derivation_mismatches: list[dict[str, Any]] = []
    group_state: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing_results: list[str] = []
    predicted_uncertain_rows: set[str] = set()
    reference_uncertain_rows: set[str] = set()

    reviewed_occurrences = [
        occurrence
        for occurrence in occurrences
        if str(occurrence["inference_id"]) in review_ids
    ]
    for occurrence in reviewed_occurrences:
        inference_id = str(occurrence["inference_id"])
        result_id = links.get(inference_id, inference_id)
        model = results.get(result_id)
        if model is None:
            missing_results.append(inference_id)
            continue
        reference_row = ledger_rows[inference_id]
        reference = _semantic_from_reference(reference_row)
        validate_invariants(model)
        validate_invariants(reference)
        group_key = (
            str(occurrence["source"]),
            str(occurrence["population_stratum"]),
            str(occurrence["gallery_depth_bucket"]),
        )
        state = group_state.setdefault(
            group_key,
            {
                "acceptable_field_decisions": 0,
                "building_ids": set(),
                "clear_field_errors": 0,
                "hero_disagreements": 0,
                "occurrences": 0,
                "total_field_decisions": 0,
                "uncertainty_false_negatives": 0,
            },
        )
        state["building_ids"].add(str(occurrence["selection_id"]))
        state["occurrences"] += 1

        for field in QA_FIELDS:
            model_value = model[field]
            reference_value = reference_row["primary"][field]
            accepted = reference_row["acceptable_values"][field]
            metric = field_counts[field]
            metric["support"] += 1
            metric["confusion"][(reference_value, model_value)] += 1
            metric["strict"] += model_value == reference_value
            is_acceptable = model_value in accepted
            metric["acceptable"] += is_acceptable
            state["total_field_decisions"] += 1
            state["acceptable_field_decisions"] += is_acceptable
            if not is_acceptable:
                detail = {
                    "acceptable": accepted,
                    "gallery_depth_bucket": occurrence["gallery_depth_bucket"],
                    "inference_id": inference_id,
                    "model": model_value,
                    "population_stratum": occurrence["population_stratum"],
                    "reference": reference_value,
                    "source": occurrence["source"],
                }
                metric["clear_errors"].append(detail)
                state["clear_field_errors"] += 1

        model_uncertain = set(model["uncertain_axes"])
        reference_uncertain = set(reference["uncertain_axes"])
        if model_uncertain:
            predicted_uncertain_rows.add(inference_id)
        if reference_uncertain:
            reference_uncertain_rows.add(inference_id)
        for axis in UNCERTAIN_AXES:
            predicted = axis in model_uncertain
            expected = axis in reference_uncertain
            key = "tp" if predicted and expected else "fp" if predicted else "fn" if expected else "tn"
            uncertainty_counts[axis][key] += 1
            if key in {"fp", "fn"}:
                uncertainty_counts[axis][f"{key}_rows"].append(inference_id)
            if key == "fn":
                state["uncertainty_false_negatives"] += 1
        predicted_resolution = bool(model["resolution_insufficient"])
        expected_resolution = bool(reference["resolution_insufficient"])
        rkey = (
            "tp"
            if predicted_resolution and expected_resolution
            else "fp"
            if predicted_resolution
            else "fn"
            if expected_resolution
            else "tn"
        )
        resolution_counts[rkey] += 1
        if rkey in {"fp", "fn"}:
            resolution_counts[f"{rkey}_rows"].append(inference_id)

        model_slots[inference_id] = derive_coverage_slots(model)
        reference_slots[inference_id] = derive_coverage_slots(reference)
        derived_model_hero = derive_hero_decision(model)[0]
        model_hero = stored_hero.get(inference_id, stored_hero.get(result_id, derived_model_hero))
        if model_hero != derived_model_hero:
            hero_derivation_mismatches.append(
                {
                    "derived": derived_model_hero,
                    "inference_id": inference_id,
                    "stored": model_hero,
                }
            )
        reference_hero = derive_hero_decision(reference)[0]
        hero_confusion[(reference_hero, model_hero)] += 1
        if reference_hero != model_hero:
            hero_disagreements.append(
                {
                    "inference_id": inference_id,
                    "model": model_hero,
                    "reference": reference_hero,
                }
            )
            state["hero_disagreements"] += 1

    fields_output: dict[str, Any] = {}
    for field, metric in field_counts.items():
        fields_output[field] = {
            "acceptable_agreement": _ratio(metric["acceptable"], metric["support"]),
            "acceptable_count": metric["acceptable"],
            "clear_error_count": len(metric["clear_errors"]),
            "clear_errors": metric["clear_errors"],
            "confusion": _confusion_records(metric["confusion"]),
            "strict_agreement": _ratio(metric["strict"], metric["support"]),
            "strict_count": metric["strict"],
            "support": metric["support"],
        }

    uncertainty_output: dict[str, Any] = {}
    total_uncertain_expected = 0
    total_uncertain_predicted = 0
    for axis, metric in uncertainty_counts.items():
        summary = _uncertainty_metric(
            metric["tp"], metric["fp"], metric["fn"], metric["tn"]
        )
        summary["fn_rows"] = metric["fn_rows"]
        summary["fp_rows"] = metric["fp_rows"]
        uncertainty_output[axis] = summary
        total_uncertain_expected += metric["tp"] + metric["fn"]
        total_uncertain_predicted += metric["tp"] + metric["fp"]
    resolution_output = _uncertainty_metric(
        resolution_counts["tp"],
        resolution_counts["fp"],
        resolution_counts["fn"],
        resolution_counts["tn"],
    )
    resolution_output["fn_rows"] = resolution_counts["fn_rows"]
    resolution_output["fp_rows"] = resolution_counts["fp_rows"]

    group_rows: list[dict[str, Any]] = []
    for (source, stratum, depth), state in sorted(group_state.items()):
        group_rows.append(
            {
                "acceptable_field_agreement": _ratio(
                    state["acceptable_field_decisions"],
                    state["total_field_decisions"],
                ),
                "acceptable_field_decisions": state["acceptable_field_decisions"],
                "building_count": len(state["building_ids"]),
                "clear_field_errors": state["clear_field_errors"],
                "gallery_depth_bucket": depth,
                "hero_disagreements": state["hero_disagreements"],
                "occurrences": state["occurrences"],
                "population_stratum": stratum,
                "source": source,
                "total_field_decisions": state["total_field_decisions"],
                "uncertainty_false_negatives": state[
                    "uncertainty_false_negatives"
                ],
            }
        )

    size_after = semantic_db.stat().st_size
    sha_after = _file_sha256(semantic_db)
    sidecars_after = _sqlite_sidecars(semantic_db)
    if (size_before, sha_before) != (size_after, sha_after):
        raise RuntimeError("semantic DB changed during immutable inspection")
    result: dict[str, Any] = {
        "accounting": {
            "buildings": len(buildings),
            "frozen_reference_rows": len(ledger_rows),
            "missing_result_rows": len(missing_results),
            "occurrences": len(occurrences),
            "reviewed_occurrences": len(reviewed_occurrences),
            "semantic_results": len(results),
            "unrendered_occurrences": len(unrendered_occurrence_ids),
        },
        "authoritative": False,
        "coverage": {
            "model": _coverage_summary(
                buildings=buildings,
                occurrences=reviewed_occurrences,
                slots_by_id=model_slots,
            ),
            "reviewer_primary": _coverage_summary(
                buildings=buildings,
                occurrences=reviewed_occurrences,
                slots_by_id=reference_slots,
            ),
        },
        "fields": fields_output,
        "hero": {
            "critical_false_positive_rows": [
                row
                for row in hero_disagreements
                if row["model"] in {"preferred", "eligible"}
                and row["reference"] in {"qa_only", "archive_only", "rejected"}
            ],
            "derivation_mismatches": hero_derivation_mismatches,
            "disagreement_count": len(hero_disagreements),
            "disagreements": hero_disagreements,
            "primary_confusion": _confusion_records(hero_confusion),
        },
        "inputs": {
            "frozen_ledger": {
                "ledger_sha256": ledger["ledger_sha256"],
                "path": str(ledger_path.resolve()),
                "reference_type": "agent_reference",
            },
            "review_manifest": {
                "byte_sha256": review_manifest_byte_sha,
                "manifest_sha256": review_manifest_self_sha,
                "path": str(review_manifest_path.resolve()),
            },
            "semantic_db": {
                "path": str(semantic_db),
                "sha256_after": sha_after,
                "sha256_before": sha_before,
                "sidecars_after": sidecars_after,
                "sidecars_before": sidecars_before,
                "size_bytes_after": size_after,
                "size_bytes_before": size_before,
            },
        },
        "inspector_version": INSPECTOR_VERSION,
        "missing_result_inference_ids": sorted(missing_results),
        "unrendered_occurrence_inference_ids": unrendered_occurrence_ids,
        "operations": {
            "db_writes": 0,
            "llm_requests": 0,
            "network_requests": 0,
            "vision_requests": 0,
        },
        "reference": {
            "completed_at": ledger["completed_at"],
            "description": "source-blind Codex agent reference; not independent human gold",
            "reviewer_id": ledger["reviewer_id"],
            "type": "agent_reference",
        },
        "run": {
            "building_count": int(run["building_count"]),
            "contract_version": str(run["contract_version"]),
            "logical_sha256": run["logical_sha256"],
            "model": str(run["model"]),
            "occurrence_count": int(run["occurrence_count"]),
            "prompt_version": str(run["prompt_version"]),
            "run_id": str(run["run_id"]),
            "status": str(run["status"]),
        },
        "runtime": runtime,
        "source_stratum_depth": group_rows,
        "uncertainty": {
            "axes": uncertainty_output,
            "expected_axis_positive_count": total_uncertain_expected,
            "expected_row_count": len(reference_uncertain_rows),
            "expected_row_rate": _ratio(
                len(reference_uncertain_rows), len(reviewed_occurrences)
            ),
            "predicted_axis_positive_count": total_uncertain_predicted,
            "predicted_row_count": len(predicted_uncertain_rows),
            "predicted_row_rate": _ratio(
                len(predicted_uncertain_rows), len(reviewed_occurrences)
            ),
            "predicted_near_zero_while_reference_material": (
                len(predicted_uncertain_rows)
                < max(1, math.ceil(len(reviewed_occurrences) * 0.01))
                and len(reference_uncertain_rows)
                >= math.ceil(len(reviewed_occurrences) * 0.05)
            ),
            "resolution_insufficient": resolution_output,
        },
        "validation": {
            "foreign_key_violations": foreign_keys,
            "frozen_before_join": True,
            "integrity_check": integrity,
            "quick_check": quick,
            "semantic_db_unchanged": sha_before == sha_after,
        },
    }
    result["inspection_sha256"] = canonical_sha256(
        {"domain": INSPECTION_DOMAIN, "inspection": result}
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, freeze, or inspect a blind semantic Vision N100 reference."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    template = subparsers.add_parser("template")
    template.add_argument("--review-manifest", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    template.add_argument("--reviewer-id", required=True)
    template.add_argument("--created-at")

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--review-manifest", type=Path, required=True)
    freeze.add_argument("--draft", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--completed-at")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--semantic-db", type=Path, required=True)
    inspect.add_argument("--review-manifest", type=Path, required=True)
    inspect.add_argument("--ledger", type=Path, required=True)
    inspect.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        result = create_ledger_template(
            review_manifest_path=args.review_manifest,
            output_path=args.output,
            reviewer_id=args.reviewer_id,
            created_at=args.created_at,
        )
    elif args.command == "freeze":
        result = freeze_ledger(
            review_manifest_path=args.review_manifest,
            draft_path=args.draft,
            output_path=args.output,
            completed_at=args.completed_at,
        )
    else:
        result = inspect_frozen_ledger(
            semantic_db=args.semantic_db,
            review_manifest_path=args.review_manifest,
            ledger_path=args.ledger,
        )
        if args.output is not None:
            _write_no_clobber(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
